import abc
from functools import partial

import jax

from ..utils.gym import RayEnvStepper, StayInTerminalStateWrapper, AutoResetWrapper
from ..utils import pytree
from ..utils.profiling import timeit


class Trainer(abc.ABC):
    def __init__(
            self, agent, env_factory, num_envs, num_collection_steps, num_workers,
            allow_to_act_in_terminal_state_once=False):
        self._agent = agent
        self._num_collection_steps = num_collection_steps
        self._num_envs = num_envs

        def _env_factory():
            env = env_factory()
            if allow_to_act_in_terminal_state_once:
                # Allow to act in terminal states
                env = StayInTerminalStateWrapper(env)
            # Automatically reset after acting in terminal state once
            env = AutoResetWrapper(env)
            return env

        self._batched_env_stepper = RayEnvStepper(_env_factory, num_envs, num_workers)
        self._current_episode_indices = list(range(num_envs))
        self._next_episode_index = num_envs

        self._agent_memory = self._agent.init_memory_batch(num_envs)
        self._current_state_batch_np = self._batched_env_stepper.reset()

    @property
    def agent(self):
        return self._agent

    @property
    def num_collection_steps(self):
        return self._num_collection_steps

    def run_training_step(self, stats=None):
        # Collect some trajectories by interacting with the environment. We call it a "day" stage.
        trajectory_batch = self._run_day(stats)
        # Update the parameters of the agent. We call it a "night" stage.
        self._run_night(stats, trajectory_batch)

    def _run_day(self, stats):
        transition_batches = []

        for step in range(self.num_collection_steps):
            action_batch_jax, act_metadata_batch_jax = self.agent.act_on_batch(
                self._current_state_batch_np, self._agent_memory)
            # Copy actions back to CPU because indexing GPU memory will slow everything down significantly
            action_batch_np = pytree.to_numpy(action_batch_jax)
            reward_done_next_state_batch_np = self._batched_env_stepper.step(action_batch_np)
            reward_done_next_state_batch_jax = pytree.to_jax(reward_done_next_state_batch_np)

            for env_index in range(self._num_envs):
                if stats is not None:
                    stats.add_transition(
                        self._current_episode_indices[env_index],
                        action_batch_np[env_index].item(),
                        reward_done_next_state_batch_np['rewards'][env_index].item(),
                        reward_done_next_state_batch_np['done'][env_index].item(),
                    )

                if reward_done_next_state_batch_np['done'][env_index]:
                    # Allocate a new episode index for stats accumulation
                    self._current_episode_indices[env_index] = self._next_episode_index
                    self._next_episode_index += 1

            transition_batches.append(
                dict(
                    memory_before=self._agent_memory,
                    current_state=self._current_state_batch_np,
                    actions=action_batch_jax,
                    act_metadata=act_metadata_batch_jax,
                    **reward_done_next_state_batch_jax,
                )
            )

            self._agent_memory = self.agent.update_memory_batch(
                prev_memory=self._agent_memory,
                new_memory_state=act_metadata_batch_jax.get('memory_state_after'),
                actions=action_batch_jax,
                done=reward_done_next_state_batch_jax['done'],
            )

            self._current_state_batch_np = reward_done_next_state_batch_np['next_state']

        return self._stack_transition_batches(transition_batches)

    @partial(jax.jit, static_argnums=(0,))
    def _stack_transition_batches(self, transition_batches):
        # Stack along timestamp dimension, return as GPU tensors
        return pytree.stack(transition_batches, axis=1, result_backend='jax')

    @abc.abstractmethod
    def _run_night(self, stats, collected_trajectories):
        pass


class DummyTrainer(Trainer):
    def _run_night(self, stats, collected_trajectories):
        pass


class OnPolicyTrainer(Trainer):
    def _run_night(self, stats, collected_trajectories):
        training_stats = self.agent.train_on_batch(collected_trajectories)
        stats.add_rolling_stats(training_stats)
