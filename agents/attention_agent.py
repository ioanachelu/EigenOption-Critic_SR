import numpy as np
import tensorflow as tf
from tools.agent_utils import get_mode, update_target_graph_aux, update_target_graph_sf, \
  update_target_graph_option, discount, reward_discount, set_image, make_gif
import os

import matplotlib.patches as patches
import matplotlib.pylab as plt
import numpy as np
from collections import deque
import seaborn as sns

sns.set()
import random
import matplotlib.pyplot as plt
from agents.eigenoc_agent_dynamic import EigenOCAgentDyn
import copy
from threading import Barrier, Thread

FLAGS = tf.app.flags.FLAGS

"""This Agent is a specialization of the successor representation direction based agent with buffer SR matrix, but instead of choosing from discreate options that are grounded in the SR basis only by means of the pseudo-reward, it keeps a singly intra-option policy whose context is changed by means of the option given as embedding (the embedding being the direction given by the spectral decomposition of the SR matrix)"""
class AttentionAgent(EigenOCAgentDyn):
  def __init__(self, sess, game, thread_id, global_step, global_episode, config, global_network, barrier):
    super(AttentionAgent, self).__init__(sess, game, thread_id, global_step, global_episode, config, global_network, barrier)
    self.episode_mean_eigen_values = []

  """Starting point of the agent acting in the environment"""
  def play(self, coord, saver):
    self.saver = saver

    with self.sess.as_default(), self.sess.graph.as_default():
      self.init_agent()

      with coord.stop_on_exception():
        while not coord.should_stop():
          if (self.config.steps != -1 and \
                  (self.global_step_np > self.config.steps and self.name == "worker_0")) or \
              (self.global_episode_np > len(self.config.goal_locations) * self.config.move_goal_nb_of_ep and
                   self.name == "worker_0" and self.config.multi_task):
            coord.request_stop()
            return 0

          """update local network parameters from global network"""
          self.sync_threads()

          self.init_episode()
          self.episode_mixed_reward = 0
          self.episode_eigen_values = []

          """Reset the environment and get the initial state"""
          s = self.env.reset()

          """While the episode does not terminate"""
          while not self.done:
            """update local network parameters from global network"""
            self.sync_threads()

            """Choose an action from the current intra-option policy"""
            self.policy_evaluation(s, self.episode_length == 0)

            s1, r, self.done, self.s1_idx = self.env.step(self.action)

            self.episode_reward += r
            self.reward = np.clip(r, -1, 1)

            """If the episode ended make the last state absorbing"""
            if self.done:
              s1 = s
              self.s1_idx = self.s_idx

            """If the next state prediction buffer is full override the oldest memories"""
            if len(self.aux_episode_buffer) == self.config.memory_size:
              self.aux_episode_buffer.popleft()
            if self.config.history_size <= 3:
              self.aux_episode_buffer.append([s, s1, self.action])
            else:
              self.aux_episode_buffer.append([s, s1[:, :, -2:-1], self.action])

            self.episode_buffer_sf.append([s, s1, self.action, self.reward, self.fi])
            self.sf_prediction(s1)

            """If the experience buffer has sufficient experience in it, every so often do an update with a batch of transition from it for next state prediction"""
            self.next_frame_prediction()

            """Do n-step prediction for the returns"""
            r_mix = self.option_prediction(s, s1)
            self.episode_mixed_reward += r_mix
            # r_mix = 0

            if self.total_steps % self.config.step_summary_interval == 0 and self.name == 'worker_0':
              self.write_step_summary(r, r_mix)

            s = s1
            self.s_idx = self.s1_idx
            self.episode_length += 1
            self.total_steps += 1

            if self.name == "worker_0":
              self.sess.run(self.increment_global_step)
              self.global_step_np = self.global_step.eval()

          self.update_episode_stats()

          if self.name == "worker_0":
            self.sess.run(self.increment_global_episode)
            self.global_episode_np = self.global_episode.eval()

            if self.global_episode_np % self.config.checkpoint_interval == 0:
              self.save_model()

            if self.global_episode_np % self.config.summary_interval == 0:
              self.write_summaries()

          """If it's time to change the task - move the goal, wait for all other threads to finish the current task"""
          if self.total_episodes % self.config.move_goal_nb_of_ep == 0 and \
                  self.total_episodes != 0:
            tf.logging.info("Moving GOAL....")
            self.barrier.wait()
            self.goal_position = self.env.set_goal(self.total_episodes, self.config.move_goal_nb_of_ep)

          self.total_episodes += 1

  """Check is the option terminates at the next state"""
  def option_terminate(self, s1):
    """If we took a primitive option, termination is assured"""
    if self.config.include_primitive_options and self.primitive_action:
      self.o_term = True
    else:
      feed_dict = {self.local_network.observation: [s1],
                   self.local_network.option_direction_placeholder: [self.global_network.directions[self.option]]}
      o_term = self.sess.run(self.local_network.termination, feed_dict=feed_dict)
      self.prob_terms = [o_term[0]]
      self.o_term = o_term[0] > np.random.uniform()

    """Stats for tracking option termination"""
    self.termination_counter += self.o_term * (1 - self.done)
    self.episode_oterm.append(self.o_term)
    self.o_tracker_len[self.option].append(self.crt_op_length)

  """Sample an action from the current option's policy"""
  def policy_evaluation(self, s, compute_svd):

    feed_dict = {self.local_network.observation: [s],
                 }
    if compute_svd:
      feed_dict[self.local_network.matrix_sf] = [self.global_network.sf_matrix_buffer]
    else:
      feed_dict[self.local_network.eigenvectors] = self.global_network.eigenvectors

    tensor_results = {"fi": self.local_network.fi,
                   "sf": self.local_network.sf,
                   "option_direction": self.local_network.current_option_direction,
                   "eigen_value": self.local_network.eigen_val,
                   "option_policy": self.local_network.option_policy,
                   "value": self.local_network.value}
    if compute_svd:
      tensor_results["eigenvectors"] = self.local_network.eigenvectors

    try:
      results = self.sess.run(tensor_results, feed_dict=feed_dict)
    except:
      print("pam pam")

    self.fi = results["fi"][0]
    sf = results["sf"][0]
    """Add the eigen option-value function to the buffer in order to add stats to tensorboad at the end of the episode"""
    self.add_SF(sf)

    self.current_option_direction = results["option_direction"][0]
    self.eigen_value =  results["eigen_value"][0]
    self.value = results["value"][0]
    pi = results["option_policy"][0]

    if compute_svd:
      self.global_network.eigenvectors = results["eigenvectors"]

    self.episode_eigen_values.append(self.eigen_value)

    if np.isnan(self.current_option_direction[0]):
      print("NAN error")

    """Sample an action"""
    self.action = np.random.choice(pi, p=pi)
    self.action = np.argmax(pi == self.action)

    ###### EXECUTE RANDOM ACTION TODO ####
    if self.config.test_random_action:
      self.action = np.random.choice(range(self.action_size))



    """Store information in buffers for stats in tensorboard"""
    self.episode_actions.append(self.action)

  """Do n-step prediction for the returns and update the option policies and critics"""
  def option_prediction(self, s, s1):
    """construct the mixed reward signal to pass to the eigen intra-option critics."""
    feed_dict = {self.local_network.observation: np.stack([s, s1])}
    fi = self.sess.run(self.local_network.fi,
                       feed_dict=feed_dict)
    """The internal reward will be the cosine similary between the direction in latent space and the 
         eigen direction corresponding to the current option"""
    r_i = self.cosine_similarity((fi[1] - fi[0]), self.current_option_direction)
    assert r_i <= 1 and r_i >= -1
    r_mix = self.config.alpha_r * r_i + (1 - self.config.alpha_r) * self.reward

    """Adding to the transition buffer for doing n-step prediction on critics and policies"""
    self.episode_buffer_option.append(
      [s, self.current_option_direction, self.action, self.reward, r_mix, s1])

    if len(self.episode_buffer_option) >= self.config.max_update_freq or self.done or (
          self.o_term and len(self.episode_buffer_option) >= self.config.min_update_freq):
      """Get the bootstrap option-value functions for the next time step"""
      if self.done:
        bootstrap_eigen_V = 0
        bootstrap_V = 0
      else:
        try:
          feed_dict = {self.local_network.observation: [s1],
                       # self.local_network.matrix_sf: [self.global_network.sf_matrix_buffer],
                       self.local_network.eigenvectors: self.global_network.eigenvectors}

          v, eigen_v = self.sess.run([self.local_network.value, self.local_network.eigen_val], feed_dict=feed_dict)
        except:
          print("stop exec")
        bootstrap_V = v[0]
        bootstrap_eigen_V = eigen_v[0]

      self.train_option(bootstrap_V, bootstrap_eigen_V)

      self.episode_buffer_option = []

    return r_mix

  """Do n-step prediction for the successor representation latent and an update for the representation latent using 1-step next frame prediction"""
  def sf_prediction(self, s1):
    if len(self.episode_buffer_sf) == self.config.max_update_freq or self.done:
      """Get the successor features of the next state for which to bootstrap from"""
      feed_dict = {self.local_network.observation: [s1]}
      next_sf = self.sess.run(self.local_network.sf,
                         feed_dict=feed_dict)[0]
      bootstrap_sf = np.zeros_like(next_sf) if self.done else next_sf
      self.train_sf(bootstrap_sf)
      self.episode_buffer_sf = []

  """Do one n-step update for training the agent's latent successor representation space and an update for the next frame prediction"""
  def train_sf(self, bootstrap_sf):
    rollout = np.array(self.episode_buffer_sf)
    observations = rollout[:, 0]
    next_observations = rollout[:, 1]
    actions = rollout[:, 2]
    rewards = rollout[:, 3]
    fi = rollout[:, 4]

    """Construct list of latent representations for the entire trajectory"""
    sf_plus = np.asarray(fi.tolist() + [bootstrap_sf])
    """Construct the targets for the next step successor representations for the entire trajectory"""
    discounted_sf = discount(sf_plus, self.config.discount)[:-1]

    feed_dict = {self.local_network.target_sf: np.stack(discounted_sf, axis=0),
                 self.local_network.observation: np.stack(observations, axis=0),
                 self.local_network.actions_placeholder: actions,
                 self.local_network.target_next_obs: np.stack(next_observations, axis=0)}

    # _, self.summaries_sf, sf_loss, _, self.summaries_aux, aux_loss = \
    _, self.summaries_sf, sf_loss = \
      self.sess.run([self.local_network.apply_grads_sf,
                     self.local_network.merged_summary_sf,
                     self.local_network.sf_loss,
                     # self.local_network.apply_grads_aux,
                     # self.local_network.merged_summary_aux,
                     # self.local_network.aux_loss
                     ],
                    feed_dict=feed_dict)

  """Do one minibatch update over the next frame prediction network"""
  def train_aux(self):
    minibatch = random.sample(self.aux_episode_buffer, self.config.batch_size)
    rollout = np.array(minibatch)
    observations = rollout[:, 0]
    next_observations = rollout[:, 1]
    actions = rollout[:, 2]

    feed_dict = {self.local_network.observation: np.stack(observations, axis=0),
                 self.local_network.target_next_obs: np.stack(next_observations, axis=0),
                 self.local_network.actions_placeholder: actions}

    aux_loss, _, self.summaries_aux = \
      self.sess.run([self.local_network.aux_loss, self.local_network.apply_grads_aux,
                     self.local_network.merged_summary_aux],
                    feed_dict=feed_dict)

  """Do n-step prediction on the critics and policies"""
  def train_option(self, bootstrap_value, bootstrap_value_mix):
    rollout = np.array(self.episode_buffer_option)
    observations = rollout[:, 0]
    option_directions = rollout[:, 1]
    actions = rollout[:, 2]
    rewards = rollout[:, 3]
    eigen_rewards = rollout[:, 4]
    next_observations = rollout[:, 5]

    """Construct list of discounted returns for the entire n-step trajectory"""
    rewards_plus = np.asarray(rewards.tolist() + [bootstrap_value])
    discounted_returns = reward_discount(rewards_plus, self.config.discount)[:-1]

    """Construct list of discounted returns using mixed reward signals for the entire n-step trajectory"""
    eigen_rewards_plus = np.asarray(eigen_rewards.tolist() + [bootstrap_value_mix])
    discounted_eigen_returns = reward_discount(eigen_rewards_plus, self.config.discount)[:-1]

    feed_dict = {self.local_network.target_return: discounted_returns,
                 self.local_network.target_eigen_return: discounted_eigen_returns,
                 self.local_network.observation: np.stack(observations, axis=0),
                 self.local_network.actions_placeholder: actions,
                 # self.local_network.matrix_sf: [self.global_network.sf_matrix_buffer],
                 self.local_network.eigenvectors: self.global_network.eigenvectors,
                 # self.local_network.current_option_direction: option_directions,
                 }

    """Do an update on the intra-option policies"""
    try:
      _, _, self.summaries_option, self.summaries_critic = self.sess.run([self.local_network.apply_grads_option,
                                                                          self.local_network.apply_grads_critic,
                                                                          self.local_network.merged_summary_option,
                                                                          self.local_network.merged_summary_critic,
                                                                          ], feed_dict=feed_dict)
      # _, _, _, \
      # self.summaries_option,\
      # self.summaries_critic,\
      # self.summaries_direction = \
      #   self.sess.run([self.local_network.apply_grads_option,
      #                  self.local_network.apply_grads_critic,
      #                  self.local_network.apply_grads_direction,
      #                  self.local_network.merged_summary_option,
      #                  self.local_network.merged_summary_critic,
      #                  self.local_network.merged_summary_direction
      #                  ], feed_dict=feed_dict)
    except:
      print("eerrere")

    """Store the bootstrap target returns at the end of the trajectory"""
    self.eigen_R = discounted_eigen_returns[-1]
    self.R = discounted_returns[-1]

  def write_step_summary(self, r, r_mix=None):
    self.summary = tf.Summary()
    self.summary.value.add(tag='Step/Action', simple_value=self.action)
    self.summary.value.add(tag='Step/MixedReward', simple_value=r_mix)
    self.summary.value.add(tag='Step/Reward', simple_value=r)
    self.summary.value.add(tag='Step/EigenV', simple_value=self.eigen_value)
    self.summary.value.add(tag='Step/V', simple_value=self.value)
    self.summary.value.add(tag='Step/Target_Eigen_Return', simple_value=self.eigen_R)
    self.summary.value.add(tag='Step/Target_Return', simple_value=self.R)

    self.summary_writer.add_summary(self.summary, self.total_steps)
    self.summary_writer.flush()

  def update_episode_stats(self):
    if len(self.episode_eigen_values) != 0:
      self.episode_mean_eigen_values.append(np.mean(self.episode_eigen_values))
    if len(self.episode_actions) != 0:
      self.episode_mean_actions.append(get_mode(self.episode_actions))

  def write_summaries(self):
    self.summary = tf.Summary()
    self.summary.value.add(tag='Perf/Return', simple_value=float(self.episode_reward))
    self.summary.value.add(tag='Perf/MixedReturn', simple_value=float(self.episode_mixed_reward))
    self.summary.value.add(tag='Perf/Length', simple_value=float(self.episode_length))

    for sum in [self.summaries_sf, self.summaries_aux, self.summaries_critic, self.summaries_option]:
      if sum is not None:
        self.summary_writer.add_summary(sum, self.global_episode_np)

    if len(self.episode_mean_eigen_values) != 0:
      last_mean_eigen_value = np.mean(self.episode_mean_eigen_values[-self.config.step_summary_interval:])
      self.summary.value.add(tag='Perf/EigenValue', simple_value=float(last_mean_eigen_value))
    if len(self.episode_mean_actions) != 0:
      last_frequent_action = self.episode_mean_actions[-1]
      self.summary.value.add(tag='Perf/FreqActions', simple_value=last_frequent_action)

    self.summary_writer.add_summary(self.summary, self.global_episode_np)
    self.summary_writer.flush()

  def eval(self, coord, saver):

    with self.sess.as_default(), self.sess.graph.as_default():
      self.init_agent()
      tf.logging.info("Starting eval agent")
      ep_rewards = []
      ep_lengths = []
      episode_frames = []

      for i in range(self.config.nb_test_ep):
        episode_reward = 0
        """Reset the environment and get the initial state"""
        s = self.env.reset()

        self.done = False
        episode_length = 0
        """While the episode does not terminate"""
        while not self.done:
          """Choose an action from the current intra-option policy"""
          self.policy_evaluation(s, self.episode_length == 0)

          feed_dict = {self.local_network.observation: np.stack([s])}
          options, o_term = self.sess.run([self.local_network.options, self.local_network.termination],
                                          feed_dict=feed_dict)

          if primitive_action:
            action = option - self.nb_options
            o_term = True
          else:
            pi = options[0, option]
            action = np.random.choice(pi, p=pi)
            action = np.argmax(pi == action)
            o_term = o_term[0, option] > np.random.uniform()

          # episode_frames.append(set_image(s, option, action, episode_length, primitive_action))
          s1, r, d, _ = self.env.step(action)

          r = np.clip(r, -1, 1)
          episode_reward += r
          episode_length += 1

          if not d and (o_term or primitive_action):
            feed_dict = {self.local_network.observation: np.stack([s1])}
            option, primitive_action = self.sess.run(
              [self.local_network.max_options, self.local_network.primitive_action], feed_dict=feed_dict)
            option, primitive_action = option[0], primitive_action[0]
            primitive_action = option >= self.config.nb_options
          s = s1
          if episode_length > self.config.max_length_eval:
            break

        ep_rewards.append(episode_reward)
        ep_lengths.append(episode_length)
        tf.logging.info("Ep {} finished in {} steps with reward {}".format(i, episode_length, episode_reward))
      # images = np.array(episode_frames)
      # make_gif(images, os.path.join(self.test_path, 'test_episodes.gif'),
      #          duration=len(images) * 1.0, true_image=True)
      tf.logging.info("Won {} episodes of {}".format(ep_rewards.count(1), self.config.nb_test_ep))