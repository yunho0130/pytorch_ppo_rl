import gym
import os
import random
from itertools import chain

import numpy as np

import torch.nn.functional as F
import torch.nn as nn
import torch
import cv2

from model import *

import torch.optim as optim
from torch.multiprocessing import Pipe, Process

from collections import deque
from sklearn.utils import shuffle
#from utils import make_train_data, ActorAgent
from tensorboardX import SummaryWriter
from torch.distributions.categorical import Categorical

class RunningMeanStd(object):
    def __init__(self, epsilon=1e-4, shape=()):
        self.mean = np.zeros(shape, 'float64')
        self.var = np.ones(shape, 'float64')
        self.count = epsilon

    def update(self, x):
        batch_mean = np.mean(x, axis=0)
        batch_var = np.var(x, axis=0)
        batch_count = x.shape[0]
        self.update_from_moments(batch_mean, batch_var, batch_count)
        
    def update_from_moments(self, batch_mean, batch_var, batch_count):
        delta = batch_mean - self.mean
        total_count = self.count + batch_count

        new_mean = self.mean + delta * batch_count / total_count
        m_a = self.var * self.count
        m_b = batch_var * batch_count
        M2 = m_a + m_b + np.square(delta) * self.count * batch_count / (self.count + batch_count)
        new_var = M2 / (self.count + batch_count)

        new_count = batch_count + self.count

        self.mean = new_mean
        self.var = new_var
        self.count = new_count


class CNNRNDAgent(object):
    def __init__(self,
                 num_step,
                 use_cuda=False,
                 use_gae=True,
                 is_load=True,
                 is_save=True):

        self.predic_model = CnnRND()
        self.target_model = CnnRND()
        self.model = CNNRNDActorCriticNetwork()
        self.learning_rate = 0.00025
        self.gamma = 0.99
        self.lam = 0.95
        self.epoch = 3
        self.clip_grad_norm = 0.5
        self.ppo_eps = 0.1
        self.batch_size = 32
        self.use_gae = use_gae
        self.num_step = num_step
        self.ent_coef = 0.001
        self.output_size = 18
        self.update_proportion = 0.25
        self.device = torch.device('cuda' if use_cuda else 'cpu')
        self.optimizer = optim.Adam(list(self.model.parameters()) + list(self.predic_model.parameters()),
                                    lr=self.learning_rate)
        self.predic_model = self.predic_model.to(self.device)
        self.target_model = self.target_model.to(self.device)
        self.model = self.model.to(self.device)

    def train_model(self, s_batch, target_ext_batch, target_int_batch,
                    y_batch, adv_batch, next_obs_batch, old_policy_batch):
        s_batch = torch.Tensor(s_batch).to(self.device).float()
        target_ext_batch = torch.Tensor(target_ext_batch).to(self.device).float()
        target_int_batch = torch.Tensor(target_int_batch).to(self.device).float()
        y_batch = torch.Tensor(y_batch).to(self.device).float()
        adv_batch = torch.Tensor(adv_batch).to(self.device).float()
        next_obs_batch = torch.Tensor(next_obs_batch).to(self.device).float()
        
        sample_range = np.arange(len(s_batch))
        forward_mse = nn.MSELoss(reduction='none')

        with torch.no_grad():
            policy_old_list = torch.stack(old_policy_batch).permute(1, 0, 2).contiguous().view(-1, self.output_size).to(self.device)
            m_old = Categorical(F.softmax(policy_old_list, dim=-1))
            log_prob_old = m_old.log_prob(y_batch)

        for i in range(self.epoch):
            np.random.shuffle(sample_range)
            for j in range(int(len(s_batch)/self.batch_size)):
                sample_idx = sample_range[self.batch_size * j:self.batch_size * (j + 1)]
                predict_next_feature = self.predic_model(next_obs_batch[sample_idx])
                target_next_feature = self.target_model(next_obs_batch[sample_idx]).detach()

                forward_loss = forward_mse(predict_next_feature, target_next_feature.detach()).mean(-1).to(self.device)
                mask = torch.rand(len(forward_loss)).to(self.device).float()
                mask = (mask < self.update_proportion).type(torch.FloatTensor).to(self.device)
                forward_loss = (forward_loss * mask).sum() / torch.max(mask.sum(),torch.Tensor([1]).to(self.device))
                
                policy, value_ext, value_int = self.model(s_batch[sample_idx])
                m = Categorical(F.softmax(policy, dim=-1))
                log_prob = m.log_prob(y_batch[sample_idx])

                ratio = torch.exp(log_prob - log_prob_old[sample_idx])

                surr1 = ratio * adv_batch[sample_idx]
                surr2 = torch.clamp(ratio, 1-self.ppo_eps, 1+self.ppo_eps) * adv_batch[sample_idx]

                actor_loss = -torch.min(surr1, surr2).mean()
                critic_ext_loss = F.mse_loss(value_ext.sum(1), target_ext_batch[sample_idx])
                critic_int_loss = F.mse_loss(value_int.sum(1), target_int_batch[sample_idx])
                
                critic_loss = critic_ext_loss + critic_int_loss
                entropy = m.entropy().mean()

                self.optimizer.zero_grad()
                loss = actor_loss + 0.5 * critic_loss - self.ent_coef * entropy + forward_loss
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 0.5)
                self.optimizer.step()

    def get_intrinsic_reward(self, state):
        state = torch.Tensor(state).to(self.device).float()
        target_result = self.target_model(state)
        predic_result = self.predic_model(state)
        intrinsic_reward = (target_result - predic_result).pow(2).sum(1) / 2

        return intrinsic_reward.data.cpu().numpy()

    def get_action(self, state):
        state = torch.Tensor(state).to(self.device).float()
        policy, value_ext, value_int = self.model(state)
        action_prob = F.softmax(policy, dim=-1).data.cpu().numpy()
        action = self.random_choice_prob_index(action_prob)

        return action, value_ext.data.cpu().numpy().squeeze(), value_int.data.cpu().numpy().squeeze(), policy.detach()

    @staticmethod
    def random_choice_prob_index(p, axis=1):
        r = np.expand_dims(np.random.rand(p.shape[1 - axis]), axis=axis)
        return (p.cumsum(axis=axis) > r).argmax(axis=axis)
        

class CNNActorAgent(object):
    def __init__(
            self,
            num_step,
            gamma=0.99,
            lam=0.95,
            use_gae=True,
            use_cuda=False):
        self.model = CnnActorCriticNetwork()
        self.num_step = num_step
        self.gamma = gamma
        self.lam = lam
        self.use_gae = use_gae
        self.learning_rate = 0.00025
        self.epoch = 3
        self.clip_grad_norm = 0.5
        self.ppo_eps = 0.1
        self.batch_size = 32

        self.optimizer = optim.Adam(self.model.parameters(), lr=self.learning_rate)

        self.device = torch.device('cuda' if use_cuda else 'cpu')

        self.model = self.model.to(self.device)

    def get_action(self, state):
        state = torch.Tensor(state).to(self.device)
        state = state.float()
        policy, value = self.model(state)
        policy = F.softmax(policy, dim=-1).data.cpu().numpy()

        action = self.random_choice_prob_index(policy)

        return action

    @staticmethod
    def random_choice_prob_index(p, axis=1):
        r = np.expand_dims(np.random.rand(p.shape[1 - axis]), axis=axis)
        return (p.cumsum(axis=axis) > r).argmax(axis=axis)

    def forward_transition(self, state, next_state):
        state = torch.from_numpy(state).to(self.device)
        state = state.float()
        policy, value = self.model(state)

        next_state = torch.from_numpy(next_state).to(self.device)
        next_state = next_state.float()
        _, next_value = self.model(next_state)

        value = value.data.cpu().numpy().squeeze()
        next_value = next_value.data.cpu().numpy().squeeze()

        return value, next_value, policy

    def train_model(self, s_batch, target_batch, y_batch, adv_batch):
        s_batch = torch.FloatTensor(s_batch).to(self.device)
        target_batch = torch.FloatTensor(target_batch).to(self.device)
        y_batch = torch.LongTensor(y_batch).to(self.device)
        adv_batch = torch.FloatTensor(adv_batch).to(self.device)

        sample_range = np.arange(len(s_batch))
        with torch.no_grad():
            # for multiply advantage
            policy_old, value_old = self.model(s_batch)
            m_old = Categorical(F.softmax(policy_old, dim=-1))
            log_prob_old = m_old.log_prob(y_batch)

        for i in range(self.epoch):
            np.random.shuffle(sample_range)
            for j in range(int(len(s_batch) / self.batch_size)):
                
                sample_idx = sample_range[self.batch_size * j:self.batch_size * (j + 1)]
                policy, value = self.model(s_batch[sample_idx])
                m = Categorical(F.softmax(policy, dim=-1))
                log_prob = m.log_prob(y_batch[sample_idx])

                ratio = torch.exp(log_prob - log_prob_old[sample_idx])

                surr1 = ratio * adv_batch[sample_idx]
                surr2 = torch.clamp(
                    ratio,
                    1.0 - self.ppo_eps,
                    1.0 + self.ppo_eps) * adv_batch[sample_idx]

                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = F.mse_loss(
                    value.sum(1), target_batch[sample_idx])

                self.optimizer.zero_grad()
                loss = actor_loss + critic_loss
                loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), self.clip_grad_norm)
                self.optimizer.step()

class MlpActorAgent(object):
    def __init__(
            self,
            input_size,
            output_size,
            num_step,
            gamma=0.99,
            lam=0.95,
            use_gae=True,
            use_cuda=False):

        self.input_size = input_size
        self.output_size = output_size
        self.model = MlpActorCriticNetwork(self.input_size, self.output_size)
        self.num_step = num_step
        self.gamma = gamma
        self.lam = lam
        self.use_gae = use_gae
        self.learning_rate = 0.00025
        self.epoch = 3
        self.clip_grad_norm = 0.5
        self.ppo_eps = 0.1
        self.batch_size = 32

        self.optimizer = optim.Adam(self.model.parameters(), lr=self.learning_rate)

        self.device = torch.device('cuda' if use_cuda else 'cpu')

        self.model = self.model.to(self.device)

    def get_action(self, state):
        state = torch.Tensor(state).to(self.device)
        state = state.float()
        policy, value = self.model(state)
        policy = F.softmax(policy, dim=-1).data.cpu().numpy()

        action = self.random_choice_prob_index(policy)

        return action

    @staticmethod
    def random_choice_prob_index(p, axis=1):
        r = np.expand_dims(np.random.rand(p.shape[1 - axis]), axis=axis)
        return (p.cumsum(axis=axis) > r).argmax(axis=axis)

    def forward_transition(self, state, next_state):
        state = torch.from_numpy(state).to(self.device)
        state = state.float()
        policy, value = self.model(state)

        next_state = torch.from_numpy(next_state).to(self.device)
        next_state = next_state.float()
        _, next_value = self.model(next_state)

        value = value.data.cpu().numpy().squeeze()
        next_value = next_value.data.cpu().numpy().squeeze()

        return value, next_value, policy

    def train_model(self, s_batch, target_batch, y_batch, adv_batch):
        s_batch = torch.FloatTensor(s_batch).to(self.device)
        target_batch = torch.FloatTensor(target_batch).to(self.device)
        y_batch = torch.LongTensor(y_batch).to(self.device)
        adv_batch = torch.FloatTensor(adv_batch).to(self.device)

        sample_range = np.arange(len(s_batch))
        with torch.no_grad():
            # for multiply advantage
            policy_old, value_old = self.model(s_batch)
            m_old = Categorical(F.softmax(policy_old, dim=-1))
            log_prob_old = m_old.log_prob(y_batch)

        for i in range(self.epoch):
            np.random.shuffle(sample_range)
            for j in range(int(len(s_batch) / self.batch_size)):
                
                sample_idx = sample_range[self.batch_size * j:self.batch_size * (j + 1)]
                policy, value = self.model(s_batch[sample_idx])
                m = Categorical(F.softmax(policy, dim=-1))
                log_prob = m.log_prob(y_batch[sample_idx])

                ratio = torch.exp(log_prob - log_prob_old[sample_idx])

                surr1 = ratio * adv_batch[sample_idx]
                surr2 = torch.clamp(
                    ratio,
                    1.0 - self.ppo_eps,
                    1.0 + self.ppo_eps) * adv_batch[sample_idx]

                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = F.mse_loss(
                    value.sum(1), target_batch[sample_idx])

                self.optimizer.zero_grad()
                loss = actor_loss + critic_loss
                loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), self.clip_grad_norm)
                self.optimizer.step()

def make_train_data_rnd(reward, done, value, gamma, num_step, num_worker):
    discounted_return = np.empty([num_worker, num_step])

    # Discounted Return
    
    use_gae = True
    use_standardization = False
    gamma = 0.99
    lam = 0.95
    stable_eps = 1e-30

    if use_gae:
        gae = np.zeros_like([num_worker, ])
        for t in range(num_step - 1, -1, -1):
            delta = reward[:, t] + gamma * value[:, t + 1] * (1 - done[:, t]) - value[:, t]
            gae = delta + gamma * lam * (1 - done[:, t]) * gae

            discounted_return[:, t] = gae + value[:, t]

            # For Actor
        adv = discounted_return - value[:, :-1]

    else:
        running_add = value[:, -1]
        for t in range(num_step - 1, -1, -1):
            running_add = reward[:, t] + gamma * running_add * (1 - done[:, t])
            discounted_return[:, t] = running_add

        # For Actor
        adv = discounted_return - value[:, :-1]

    return discounted_return.reshape([-1]), adv.reshape([-1])

def make_train_data(reward, done, value, next_value):
    num_step = len(reward)
    discounted_return = np.empty([num_step])

    use_gae = True
    use_standardization = False
    gamma = 0.99
    lam = 0.95
    stable_eps = 1e-30

    # Discounted Return
    if use_gae:
        gae = 0
        for t in range(num_step - 1, -1, -1):
            delta = reward[t] + gamma * \
                next_value[t] * (1 - done[t]) - value[t]
            gae = delta + gamma * lam * (1 - done[t]) * gae

            discounted_return[t] = gae + value[t]

        # For Actor
        adv = discounted_return - value

    else:
        for t in range(num_step - 1, -1, -1):
            running_add = reward[t] + gamma * next_value[t] * (1 - done[t])
            discounted_return[t] = running_add

        # For Actor
        adv = discounted_return - value

    if use_standardization:
        adv = (adv - adv.mean()) / (adv.std() + stable_eps)

    return discounted_return, adv

class RewardForwardFilter(object):
    def __init__(self, gamma):
        self.rewems = None
        self.gamma = gamma

    def update(self, rews):
        if self.rewems is None:
            self.rewems = rews
        else:
            self.rewems = self.rewems * self.gamma + rews
        return self.rewems