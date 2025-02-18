#!/usr/bin/env python3
import os
import time
import math
import ptan
import gymnasium as gym
import argparse
from torch.utils.tensorboard.writer import SummaryWriter
from gymnasium.wrappers import RecordVideo

from lib import model_LunarLander
from lib import common_LunarLander

import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F


GAMMA = 0.99
REWARD_STEPS = 2
BATCH_SIZE = 32
LEARNING_RATE = 5e-5
ENTROPY_BETA = 1e-4


GAMMA = 0.99
LEARNING_RATE = 0.001
ENTROPY_BETA = 0.01
BATCH_SIZE = 128
NUM_ENVS = 100


TEST_ITERS = 1000


def test_net(net: model_LunarLander.ModelA2C, env: gym.Env, count: int = 10,
             device: torch.device = torch.device("cpu")):
    rewards = 0.0
    steps = 0
    for _ in range(count):
        obs, _ = env.reset()
        while True:
            obs_v = ptan.agent.float32_preprocessor([obs])
            obs_v = obs_v.to(device)
            mu_v = net(obs_v)[0]
            action = mu_v.squeeze(dim=0).data.cpu().numpy()
            action = np.clip(action, -1, 1)
            obs, reward, done, is_tr, _ = env.step(action)
            rewards += reward
            steps += 1
            if done or is_tr:
                break
    return rewards / count, steps / count


def calc_logprob(mu_v: torch.Tensor, var_v: torch.Tensor, actions_v: torch.Tensor):
    p1 = - ((mu_v - actions_v) ** 2) / (2*var_v.clamp(min=1e-3))
    p2 = - torch.log(torch.sqrt(2 * math.pi * var_v))
    return p1 + p2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", default="cpu",
                        help="Device to use, default=cpu")
    parser.add_argument("-n", "--name", required=False, help="Name of the run")
    args = parser.parse_args()
    device = torch.device(args.dev)

    save_path = os.path.join("saves", "a2c-lunarlander")
    os.makedirs(save_path, exist_ok=True)

    game = "LunarLander-v2"
    env_factories = [
        lambda: gym.make(game, continuous=True)
        for _ in range(NUM_ENVS)
    ]
    env = gym.vector.SyncVectorEnv(env_factories)
    test_env = gym.make(game, continuous=True)

    #net = model_LunarLander.ModelA2C(env.observation_space.shape[0], env.action_space.shape[0]).to(device)
    net = model_LunarLander.ModelA2C(env.observation_space.shape[1], env.action_space.shape[0]).to(device)
    print(net)

    writer = SummaryWriter(comment="-a2c_lunarlander")
    agent = model_LunarLander.AgentA2C(net, device=device)

    #exp_source = ptan.experience.ExperienceSourceFirstLast(env, agent, GAMMA, steps_count=REWARD_STEPS)
    exp_source = ptan.experience.VectorExperienceSourceFirstLast(env, agent, gamma=GAMMA, steps_count=REWARD_STEPS)

    optimizer = optim.Adam(net.parameters(), lr=LEARNING_RATE)

    batch = []
    best_reward = None
    with ptan.common.utils.RewardTracker(writer) as tracker:
        with ptan.common.utils.TBMeanTracker(writer, batch_size=10) as tb_tracker:
            for step_idx, exp in enumerate(exp_source):
                rewards_steps = exp_source.pop_rewards_steps()
                if rewards_steps:
                    rewards, steps = zip(*rewards_steps)
                    tb_tracker.track("episode_steps", steps[0], step_idx)
                    tracker.reward(rewards[0], step_idx)

                if step_idx % TEST_ITERS == 0:
                    ts = time.time()
                    rewards, steps = test_net(net, test_env, device=device)
                    print("Test done is %.2f sec, reward %.3f, steps %d" % (
                        time.time() - ts, rewards, steps))
                    writer.add_scalar("test_reward", rewards, step_idx)
                    writer.add_scalar("test_steps", steps, step_idx)
                    if best_reward is None or best_reward < rewards:
                        if best_reward is not None:
                            print("Best reward updated: %.3f -> %.3f" % (best_reward, rewards))
                            name = "best_%+.3f_%d.dat" % (rewards, step_idx)
                            fname = os.path.join(save_path, name)
                            torch.save(net.state_dict(), fname)
                        best_reward = rewards

                batch.append(exp)
                if len(batch) < BATCH_SIZE:
                    continue

                states_v, actions_v, vals_ref_v = common_LunarLander.unpack_batch_a2c(
                    batch, net, device=device, last_val_gamma=GAMMA ** REWARD_STEPS)
                batch.clear()

                optimizer.zero_grad()
                mu_v, var_v, value_v = net(states_v)

                loss_value_v = F.mse_loss(value_v.squeeze(-1), vals_ref_v)
                adv_v = vals_ref_v.unsqueeze(dim=-1) - value_v.detach()
                log_prob_v = adv_v * calc_logprob(mu_v, var_v, actions_v)
                loss_policy_v = -log_prob_v.mean()
                ent_v = -(torch.log(2*math.pi*var_v) + 1)/2
                entropy_loss_v = ENTROPY_BETA * ent_v.mean()

                loss_v = loss_policy_v + entropy_loss_v + loss_value_v
                loss_v.backward()
                optimizer.step()

                tb_tracker.track("advantage", adv_v, step_idx)
                tb_tracker.track("values", value_v, step_idx)
                tb_tracker.track("batch_rewards", vals_ref_v, step_idx)
                tb_tracker.track("loss_entropy", entropy_loss_v, step_idx)
                tb_tracker.track("loss_policy", loss_policy_v, step_idx)
                tb_tracker.track("loss_value", loss_value_v, step_idx)
                tb_tracker.track("loss_total", loss_v, step_idx)
