#!/usr/bin/python3

from pathlib import Path

import torch
from torch import nn
import torch.nn.functional as F

import numpy as np
import gymnasium as gym

import matplotlib.pyplot as plt

from policy import PPOPolicy
from ppobuffer import PPOBuffer


NUM_STEPS = 2048                    # Timesteps data to collect before updating
BATCH_SIZE = 64                     # Batch size of training data
TOTAL_TIMESTEPS = NUM_STEPS * 10  # 500   # Total timesteps to run
GAMMA = 0.99                        # Discount factor
GAE_LAM = 0.95                      # For generalized advantage estimation
NUM_EPOCHS = 10                     # Number of epochs to train
REPORT_STEPS = 1000                 # Number of timesteps between reports


class PI_Network(nn.Module):
    def __init__(self, obs_dim, action_dim, lower_bound, upper_bound) -> None:
        super().__init__()
        (
            self.lower_bound,
            self.upper_bound
        ) = (
            torch.tensor(lower_bound, dtype=torch.float32),
            torch.tensor(upper_bound, dtype=torch.float32)
        )
        self.fc1 = nn.Linear(obs_dim, 64)
        self.fc2 = nn.Linear(64, 64)
        self.fc3 = nn.Linear(64, action_dim)

    def forward(self, obs):
        y = F.tanh(self.fc1(obs))
        y = F.tanh(self.fc2(y))
        action = self.fc3(y)

        action = ((action + 1) * (self.upper_bound - self.lower_bound) / 2 +
                  self.lower_bound)

        return action


class V_Network(nn.Module):
    def __init__(self, obs_dim) -> None:
        super().__init__()

        self.fc1 = nn.Linear(obs_dim, 64)
        self.fc2 = nn.Linear(64, 64)
        self.fc3 = nn.Linear(64, 1)

    def forward(self, obs):
        y = F.tanh(self.fc1(obs))
        y = F.tanh(self.fc2(y))
        values = self.fc3(y)

        return values


if __name__ == "__main__":

    env = gym.make("Pendulum-v1")
    obs_dim = env.observation_space.shape[0]
    action_dim = env.action_space.shape[0]
    lower_bound = env.action_space.low
    upper_bound = env.action_space.high

    pi_network = PI_Network(obs_dim, action_dim, lower_bound, upper_bound)
    v_network = V_Network(obs_dim)

    learning_rate = 3e-4

    buffer = PPOBuffer(obs_dim, action_dim, NUM_STEPS)

    policy = PPOPolicy(
        pi_network,
        v_network,
        learning_rate,
        clip_range=0.2,
        value_coeff=0.5,
        obs_dim=obs_dim,
        action_dim=action_dim,
        initial_std=1.0,
        max_grad_norm=0.5,
    )

    ep_reward = 0.0
    ep_count = 0
    season_count = 0

    pi_losses, v_losses, total_losses, approx_kls, stds = [], [], [], [], []
    mean_rewards = []

    obs, _ = env.reset()

    for t in range(TOTAL_TIMESTEPS):

        if t % REPORT_STEPS == 0:
            print(t, '/', TOTAL_TIMESTEPS)

        action, log_prob, values = policy.get_action(obs)

        clipped_action = np.clip(action, lower_bound, upper_bound)

        next_obs, reward, terminated, truncated, _ = env.step(clipped_action)

        done = terminated or truncated

        ep_reward += reward

        # Add to buffer
        buffer.record(obs, action, reward, values, log_prob)

        obs = next_obs

        # Calculate advantage and returns if it is the end of episode or
        # its time to update
        if done or (t + 1) % NUM_STEPS == 0:
            if done:
                ep_count += 1
            # Value of last time-step
            last_value = policy.get_values(obs)

            # Compute returns and advantage and store in buffer
            buffer.process_trajectory(
                gamma=GAMMA,
                gae_lam=GAE_LAM,
                is_last_terminal=done,
                last_v=last_value)
            obs, _ = env.reset()

        if (t + 1) % NUM_STEPS == 0:
            season_count += 1
            # Update for epochs
            for ep in range(NUM_EPOCHS):
                batch_data = buffer.get_mini_batch(BATCH_SIZE)
                num_grads = len(batch_data)

                # Iterate over minibatch of data
                for k in range(num_grads):
                    (
                        obs_batch,
                        action_batch,
                        log_prob_batch,
                        advantage_batch,
                        return_batch,
                    ) = (
                        batch_data[k]['obs'],
                        batch_data[k]['action'],
                        batch_data[k]['log_prob'],
                        batch_data[k]['advantage'],
                        batch_data[k]['return'],
                    )

                    # Normalize advantage
                    advantage_batch = (
                        advantage_batch -
                        np.squeeze(np.mean(advantage_batch, axis=0))
                    ) / (np.squeeze(np.std(advantage_batch, axis=0)) + 1e-8)

                    # Convert to torch tensor
                    (
                        obs_batch,
                        action_batch,
                        log_prob_batch,
                        advantage_batch,
                        return_batch,
                    ) = (
                        torch.tensor(obs_batch, dtype=torch.float32),
                        torch.tensor(action_batch, dtype=torch.float32),
                        torch.tensor(log_prob_batch, dtype=torch.float32),
                        torch.tensor(advantage_batch, dtype=torch.float32),
                        torch.tensor(return_batch, dtype=torch.float32),
                    )

                    # Update the networks on minibatch of data
                    (
                        pi_loss,
                        v_loss,
                        total_loss,
                        approx_kl,
                        std,
                    ) = policy.update(obs_batch, action_batch,
                                      log_prob_batch, advantage_batch,
                                      return_batch)

                    pi_losses.append(pi_loss.numpy())
                    v_losses.append(v_loss.numpy())
                    total_losses.append(total_loss.numpy())
                    approx_kls.append(approx_kl.numpy())
                    stds.append(std.numpy())

            buffer.clear()

            mean_ep_reward = ep_reward / ep_count
            ep_reward, ep_count = 0.0, 0

            mean_rewards.append(mean_ep_reward)
            pi_losses, v_losses, total_losses, approx_kls, stds = (
                    [], [], [], [], [])

    # Save policy and value network
    Path('saved_network').mkdir(parents=True, exist_ok=True)
    torch.save(pi_network.state_dict(), 'saved_network/pi_network.pth')
    torch.save(v_network.state_dict(), 'saved_network/v_network.pth')

    # Plot episodic reward
    _, ax = plt.subplots(1, 1, figsize=(5, 4), constrained_layout=True)
    ax.plot(range(season_count), mean_rewards)
    ax.set_xlabel("season")
    ax.set_ylabel("episodic reward")
    ax.grid(True)
    plt.show()
