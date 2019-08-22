import gym
from gym import spaces
from gym.utils import seeding
from gym.envs.registration import register
import numpy as np
import heapq
import time
import random
import json
import os
import math
from simple_arg_parse import arg_or_default
from common import sender_obs, config

MAX_CWND = 5000
MIN_CWND = 4

MAX_RATE = 1000
MIN_RATE = 40

REWARD_SCALE = 0.001

#MAX_STEPS = 400
MAX_STEPS = 60000

EVENT_TYPE_SEND = 'S'
EVENT_TYPE_ACK = 'A'

BYTES_PER_PACKET = 1500

LATENCY_PENALTY = 1.0
LOSS_PENALTY = 1.0

USE_LATENCY_NOISE = True
MAX_LATENCY_NOISE = 1.1

USE_CWND = False
RANDOM_LINK = False
DUMPRATE = 100

USE_SEND_PERIOD_NOISE = True

class Link():

    def __init__(self, bandwidth, delay, queue_size, loss_rate):
        self.bw = float(bandwidth)
        self.dl = delay
        self.lr = loss_rate
        self.queue_delay = 0.0
        self.queue_delay_update_time = 0.0
        self.max_queue_delay = queue_size / self.bw

    def get_cur_queue_delay(self, event_time):
        return max(0.0, self.queue_delay - (event_time - self.queue_delay_update_time))

    def get_cur_latency(self, event_time):
        return self.dl + self.get_cur_queue_delay(event_time)

    def packet_enters_link(self, event_time):
        if (random.random() < self.lr):
            return False
        self.queue_delay = self.get_cur_queue_delay(event_time)
        self.queue_delay_update_time = event_time
        extra_delay = 1.0 / self.bw
        #print("Extra delay: %f, Current delay: %f, Max delay: %f" % (extra_delay, self.queue_delay, self.max_queue_delay))
        if extra_delay + self.queue_delay > self.max_queue_delay:
            #print("\tDrop!")
            return False
        self.queue_delay += extra_delay
        #print("\tNew delay = %f" % self.queue_delay)
        return True

    def print_debug(self):
        print("Link:")
        print("Bandwidth: %f" % self.bw)
        print("Delay: %f" % self.dl)
        print("Queue Delay: %f" % self.queue_delay)
        print("Max Queue Delay: %f" % self.max_queue_delay)
        print("One Packet Queue Delay: %f" % (1.0 / self.bw))

    def reset(self):
        self.queue_delay = 0.0
        self.queue_delay_update_time = 0.0

class Network():

    def __init__(self, senders, links):
        self.q = []
        self.cur_time = 0.0
        self.senders = senders
        self.links = links
        self.sendrate_sum = 0.0
        self.queue_initial_packets()

    def _get_send_period(self, rate):
        if USE_SEND_PERIOD_NOISE:
            rand_n = 1.0 / rate * np.random.normal(0.0, 0.1)
            # print(["get send period", 1.0 / rate, rand_n, 1.0 / rate + rand_n])
            return (1.0 / rate) + rand_n
        else:
            return 1.0 / rate

    def queue_initial_packets(self):
        for sender in self.senders:
            sender.register_network(self)
            sender.reset_obs()
            # min heap
            heapq.heappush(self.q, (self._get_send_period(sender.rate), sender, EVENT_TYPE_SEND, 0, 0.0, False))

    def reset(self):
        self.cur_time = 0.0
        self.q = []
        [link.reset() for link in self.links]
        [sender.reset() for sender in self.senders]
        self.queue_initial_packets()

    def get_cur_time(self):
        return self.cur_time


    def sigmoid(self, x):
        # return 1 / (1 + math.exp(-x))
        return 1 / (1 + math.exp(10 * x))

    def run_for_dur(self, dur):
        end_time = self.cur_time + dur
        for sender in self.senders:
            sender.reset_obs()

        while self.cur_time < end_time:
            event_time, sender, event_type, next_hop, cur_latency, dropped = heapq.heappop(self.q)
            #print("Got event %s, to link %d, latency %f at time %f" % (event_type, next_hop, cur_latency, event_time))
            self.cur_time = event_time
            new_event_time = event_time
            new_event_type = event_type
            new_next_hop = next_hop
            new_latency = cur_latency
            new_dropped = dropped
            push_new_event = False

            if event_type == EVENT_TYPE_ACK:
                if next_hop == len(sender.path):
                    if dropped:
                        sender.on_packet_lost()
                        #print("Packet lost at time %f" % self.cur_time)
                    else:
                        sender.on_packet_acked(cur_latency)
                        #print("Packet acked at time %f" % self.cur_time)
                else:
                    new_next_hop = next_hop + 1
                    link_latency = sender.path[next_hop].get_cur_latency(self.cur_time)
                    if USE_LATENCY_NOISE:
                        link_latency *= random.uniform(1.0, 1.1)
                    new_latency += link_latency
                    new_event_time += link_latency
                    push_new_event = True
            if event_type == EVENT_TYPE_SEND:
                if next_hop == 0:
                    #print("Packet sent at time %f" % self.cur_time)
                    if sender.can_send_packet():
                        sender.on_packet_sent()
                        push_new_event = True
                    heapq.heappush(self.q, (self.cur_time + self._get_send_period(sender.rate), sender, EVENT_TYPE_SEND, 0, 0.0, False))

                else:
                    push_new_event = True

                if next_hop == sender.dest:
                    new_event_type = EVENT_TYPE_ACK
                new_next_hop = next_hop + 1

                link_latency = sender.path[next_hop].get_cur_latency(self.cur_time)
                if USE_LATENCY_NOISE:
                    link_latency *= random.uniform(1.0, MAX_LATENCY_NOISE)
                new_latency += link_latency
                new_event_time += link_latency
                new_dropped = not sender.path[next_hop].packet_enters_link(self.cur_time)

            if push_new_event:
                heapq.heappush(self.q, (new_event_time, sender, new_event_type, new_next_hop, new_latency, new_dropped))

        rewards = []
        sendrate_sum = 0.0
        for sender in self.senders:
            sender_mi = sender.get_run_data()
            sendrate_sum += sender_mi.get("send rate")

        for sender in self.senders:
            sender_mi = sender.get_run_data()
            sendrate = sender_mi.get("send rate")
            throughput = sender_mi.get("recv rate")
            latency = sender_mi.get("avg latency")
            loss = sender_mi.get("loss ratio")

            # aurora:
            # reward = (10.0 * throughput / (8 * BYTES_PER_PACKET) - 1e3 * latency - 2e3 * loss) * REWARD_SCALE

            # 2e12: reward = (10.0 * throughput / (8 * BYTES_PER_PACKET) - 2e3 * loss) * REWARD_SCALE
            # 2e13: reward = (10.0 * throughput / (8 * BYTES_PER_PACKET)) * REWARD_SCALE
            # 2e14: reward = (10.0 * sendrate / (8 * BYTES_PER_PACKET) - 11.3 * 10.0 * sendrate / (8 * BYTES_PER_PACKET) * loss) * 0.0001
            # reward = (10.0 * sendrate / (8 * BYTES_PER_PACKET) - 11.3 * 10.0 * sendrate / (8 * BYTES_PER_PACKET) * loss) * 0.001
            # 2e15: reward = (10.0 * throughput / (8 * BYTES_PER_PACKET) * self.sigmoid(loss - 0.05) - 10.0 * sendrate / (8 * BYTES_PER_PACKET) * loss) * REWARD_SCALE
            # reward = (10.0 * throughput / (8 * BYTES_PER_PACKET) * self.sigmoid(loss - 0.05) - 10.0 * sendrate / (8 * BYTES_PER_PACKET) * loss) * REWARD_SCALE

            # 2e16:
            T = 10.0 * throughput / (8 * BYTES_PER_PACKET)
            X = 10.0 * sendrate / (8 * BYTES_PER_PACKET)
            # F = 0.1 * X * abs((sendrate / sendrate_sum - 1 / len(self.senders)))
            # reward = (T * self.sigmoid(loss - 0.05) - X * loss - F) * 1e-6
            reward = (T * self.sigmoid(loss - 0.05) - X * loss) * 1e-6
            # print([T * self.sigmoid(loss - 0.05), -X * loss, -F, reward*1e6])
            # print(reward)
            rewards.append(reward)

        return rewards

class Sender():

    def __init__(self, rate, path, dest, features, cwnd=25, history_len=10):
        self.id = Sender._get_next_id()
        self.starting_rate = rate
        #print(self.starting_rate)
        self.rate = rate
        self.reward = 0.0
        self.sent = 0
        self.acked = 0
        self.lost = 0
        self.bytes_in_flight = 0
        self.min_latency = None
        self.rtt_samples = []
        self.sample_time = []
        self.net = None
        self.path = path
        self.dest = dest
        self.history_len = history_len
        self.features = features

        self.history = sender_obs.SenderHistory(self.history_len,
                                                self.features, self.id)
        self.cwnd = cwnd

    _next_id = 1
    def _get_next_id():
        result = Sender._next_id
        Sender._next_id += 1
        return result

    def apply_rate_delta2(self, delta):
        if(np.isnan(delta)):
            print("delta"+str(delta))
            # printed nan
        config.DELTA_SCALE = 0.025
        # config.DELTA_SCALE = 1e6
        delta *= config.DELTA_SCALE
        if delta >= 0.0:
            self.rate = self.rate * (1.0 + delta)
        else:
            self.rate = self.rate / (1.0 - delta)
        if self.rate > 1000.0:
            self.rate = 1000.0
        elif self.rate < 80.0:
            self.rate = 80.0
        return self.rate


    def apply_cwnd_delta(self, delta):
        delta *= config.DELTA_SCALE
        #print("Applying delta %f" % delta)
        if delta >= 0.0:
            self.set_cwnd(self.cwnd * (1.0 + delta))
        else:
            self.set_cwnd(self.cwnd / (1.0 - delta))

    def can_send_packet(self):
        if USE_CWND:
            return int(self.bytes_in_flight) / BYTES_PER_PACKET < self.cwnd
        else:
            return True

    def register_network(self, net):
        self.net = net

    def on_packet_sent(self):
        self.sent += 1
        self.bytes_in_flight += BYTES_PER_PACKET

    def on_packet_acked(self, rtt):
        self.acked += 1
        self.rtt_samples.append(rtt)
        if (self.min_latency is None) or (rtt < self.min_latency):
            self.min_latency = rtt
        self.bytes_in_flight -= BYTES_PER_PACKET

    def on_packet_lost(self):
        self.lost += 1
        self.bytes_in_flight -= BYTES_PER_PACKET

    def set_cwnd(self, new_cwnd):
        self.cwnd = int(new_cwnd)
        #print("Attempt to set new rate to %f (min %f, max %f)" % (new_rate, MIN_RATE, MAX_RATE))
        if self.cwnd > MAX_CWND:
            self.cwnd = MAX_CWND
        if self.cwnd < MIN_CWND:
            self.cwnd = MIN_CWND

    def record_run(self):
        smi = self.get_run_data()
        self.history.step(smi)

    def get_obs(self):
        return self.history.as_array()


    def get_run_data(self):
        obs_end_time = self.net.get_cur_time()

        return sender_obs.SenderMonitorInterval(
            self.id,
            bytes_sent=self.sent * BYTES_PER_PACKET,
            bytes_acked=self.acked * BYTES_PER_PACKET,
            bytes_lost=self.lost * BYTES_PER_PACKET,
            send_start=self.obs_start_time,
            send_end=obs_end_time,
            recv_start=self.obs_start_time,
            recv_end=obs_end_time,
            rtt_samples=self.rtt_samples,
            packet_size=BYTES_PER_PACKET
        )

    def reset_obs(self):
        self.sent = 0
        self.acked = 0
        self.lost = 0
        self.rtt_samples = []
        self.obs_start_time = self.net.get_cur_time()

    def print_debug(self):
        print("Sender:")
        print("Obs: %s" % str(self.get_obs()))
        print("Rate: %f" % self.rate)
        print("Sent: %d" % self.sent)
        print("Acked: %d" % self.acked)
        print("Lost: %d" % self.lost)
        print("Min Latency: %s" % str(self.min_latency))

    def reset(self):
        #print("Resetting sender!")
        self.rate = self.starting_rate
        self.bytes_in_flight = 0
        self.min_latency = None
        self.reset_obs()
        self.history = sender_obs.SenderHistory(self.history_len,
                                                self.features, self.id)

    def __eq__(self, other):
        return (self.id == other.id)
    def __lt__(self, other):
        return (self.id < other.id)

class SimulatedMultAgentNetworkEnv(gym.Env):

    def __init__(self, arglist = None,
                 history_len=arg_or_default("--history-len", default=10),
                 features=arg_or_default("--input-features",
                    default="sent latency inflation,"
                          + "latency ratio,"
                          + "send ratio")):
        self.n = arglist.num_agents
        self.log_dir = arglist.log_dir
        self.save_rate = arglist.save_rate
        self.episodes_run = -1

        self._rm_log_dir()

        self.viewer = None
        self.rand = None

        self.min_bw, self.max_bw = (100, 500)
        self.min_lat, self.max_lat = (0.05, 0.5)
        self.min_queue, self.max_queue = (0, 8)
        self.min_loss, self.max_loss = (0.0, 0.05)
        # self.history_len = history_len
        self.history_len = 1
        print("History length: %d" % history_len)
        self.features = features.split(",")
        print("Features: %s" % str(self.features))

        self.links = None
        self.senders = None
        self.run_dur = None
        self.create_new_links_and_senders(RANDOM_LINK)
        self.net = Network(self.senders, self.links)

        self.run_period = 0.1

        self.max_steps = arglist.max_episode_len
        self.debug_thpt_changes = False
        self.last_thpt = None
        self.last_rate = None


        self.observation_space = []
        self.action_space = []
        self.steps_taken = [0 for _ in range(self.n)]
        self.reward_sums = [0.0 for _ in range(self.n)]
        self.reward_ewmas = [0.0 for _ in range(self.n)]
        self.event_records = [[] for _ in range(self.n)] # per episode record, list of dicts
        self.step_records = [[] for _ in range(self.n)]

        # per step count, reset in each episode
        self.agent_rewards = [[] for _ in range(self.n)]  # individual per step reward, for computing avg over steps in an episode
        self.agent_sendrates = [[] for _ in range(self.n)]  # individual per step send-rate, for computing avg
        self.agent_latencies = [[] for _ in range(self.n)]  # individual per step send-rate, for computing avg
        self.agent_throughputs = [[] for _ in range(self.n)]  # individual  per step send-rate, for computing avg
        self.agent_lossrates = [[] for _ in range(self.n)]  # individual  per step send-rate, for computing avg
        self.sum_rewards = [] # sum of rewards over all agents, for computing avg

        # per episode count, kept
        self.episode_rewards = [[] for _ in range(self.n)]
        self.episode_sendrates = [[] for _ in range(self.n)]
        self.episode_latencies = [[] for _ in range(self.n)]
        self.episode_throughputs = [[] for _ in range(self.n)]
        self.episode_lossrates = [[] for _ in range(self.n)]
        self.episode_sum_rewards = []

        for i, sender in enumerate(self.senders, 0):

            if USE_CWND:
                self.action_space.append(spaces.Box(np.array([-1e12, -1e12]), np.array([1e12, 1e12]), dtype=np.float32))
            else:
                self.action_space.append(spaces.Box(np.array([-MAX_RATE]), np.array([MAX_RATE]), dtype=np.float32))

            use_only_scale_free = True
            single_obs_min_vec = sender_obs.get_min_obs_vector(self.features)
            single_obs_max_vec = sender_obs.get_max_obs_vector(self.features)
            self.observation_space.append(spaces.Box(np.tile(single_obs_min_vec, self.history_len),
                                            np.tile(single_obs_max_vec, self.history_len),
                                            dtype=np.float32))

    def seed(self, seed=None):
        self.rand, seed = seeding.np_random(seed)
        return [seed]

    def _reset_parameters(self):
        self.steps_taken = [0 for _ in range(self.n)]
        self.agent_rewards = [[] for _ in range(self.n)]  # individual per step reward, for computing avg over steps in an episode
        self.agent_sendrates = [[] for _ in range(self.n)]  # individual per step send-rate, for computing avg
        self.agent_latencies = [[] for _ in range(self.n)]  # individual per step send-rate, for computing avg
        self.agent_throughputs = [[] for _ in range(self.n)]  # individual  per step send-rate, for computing avg
        self.agent_lossrates = [[] for _ in range(self.n)]  # individual  per step send-rate, for computing avg
        self.sum_rewards = [] # sum of rewards over all agents, for computing avg

    # add per episode record
    def _add_episode_record(self):
        for i, sender in enumerate(self.senders, 0):
            self.episode_rewards[i].append(np.sum(self.agent_rewards[i]))
            self.episode_sum_rewards.append(np.sum(self.sum_rewards))
            self.episode_sendrates[i].append(np.mean(self.agent_sendrates[i]))
            self.episode_throughputs[i].append(np.mean(self.agent_throughputs[i]))
            self.episode_latencies[i].append(np.mean(self.agent_latencies[i]))
            self.episode_lossrates[i].append(np.mean(self.agent_lossrates[i]))

    def _add_step_record(self):
        for i, sender in enumerate(self.senders, 0):
            event = {}
            event["Sender"] = (i+1)
            event["Episode"] = self.episodes_run
            event["Step"] = self.steps_taken[i]
            event["Reward"] = self.agent_rewards[i][-1]
            event["Send Rate"] = self.agent_sendrates[i][-1]
            event["Throughput"] = self.agent_throughputs[i][-1]
            event["Latency"] = self.agent_latencies[i][-1]
            event["Loss Rate"] = self.agent_lossrates[i][-1]
            self.step_records[i].append(event)

    # add event record, episodes avg
    def _add_event_record(self):
        for i, sender in enumerate(self.senders, 0):
            event = {}
            event["Sender"] = (i+1)
            #event["Name"] = "Step"
            event["Episode"] = self.episodes_run
            event["Reward"] = np.mean(self.episode_rewards[i][-self.save_rate:])
            event["SumReward"] = np.mean(self.episode_sum_rewards[-self.save_rate:]) / self.n
            event["Send Rate"] = np.mean(self.episode_sendrates[i][-self.save_rate:])
            event["Throughput"] = np.mean(self.episode_throughputs[i][-self.save_rate:])
            event["Latency"] = np.mean(self.episode_latencies[i][-self.save_rate:])
            event["Loss Rate"] = np.mean(self.episode_lossrates[i][-self.save_rate:])
            # event["Latency Inflation"] = sender_mi.get("sent latency inflation")
            # event["Latency Ratio"] = sender_mi.get("latency ratio")
            # event["Send Ratio"] = sender_mi.get("send ratio")
            self.event_records[i].append(event)


    # remove old dump-events in log_dir
    def _rm_log_dir(self):
        import shutil
        try:
            shutil.rmtree("./dump-events" + self.log_dir)
        except:
            pass

    def _get_all_sender_obs(self):
        sender_obs = []
        sendrate_sum = 0.0
        for sender in self.senders:
            sendrate_sum += sender.rate

        for sender in self.senders:
            # sender_obs.append(np.array(sender.rate / sendrate_sum, self.links[0].bw / sendrate_sum))
            sender_obs.append(np.array(sender.get_obs()).reshape(-1,))
        return sender_obs

    def step(self, actions):
        for i in range(self.n):
            action = actions[i]
            self.senders[i].rate = self.senders[i].apply_rate_delta2(action[0])
            if USE_CWND:
                self.senders[i].rate = self.senders[i].apply_cwnd_delta2(action[1])

        reward_n = self.net.run_for_dur(self.run_dur)

        for sender in self.senders:
            sender.record_run()

        obs_n = self._get_all_sender_obs()

        #if event["Latency"] > 0.0:
        # self.run_dur = 2 * self.max_lat # 1.0
        self.run_dur = 2.0

        # per step record of the attributes
        self.sum_rewards.append(0.0)
        for i, sender in enumerate(self.senders, 0):
            self.reward_sums[i] += reward_n[i]
            self.steps_taken[i] += 1

            sender_mi = sender.get_run_data()

            self.agent_rewards[i].append(reward_n[i])

            self.agent_sendrates[i].append(sender.rate)
            #self.agent_sendrates[i].append(sender_mi.get("send rate"))
            self.agent_latencies[i].append(sender_mi.get("avg latency"))
            self.agent_throughputs[i].append(sender_mi.get("recv rate"))
            self.agent_lossrates[i].append(sender_mi.get("loss ratio"))
            self.sum_rewards[-1] += reward_n[i]

        if self.episodes_run > 0 and self.episodes_run % 1000 == 0:
            self._add_step_record()

        return obs_n, reward_n, [((self.steps_taken[i] >= self.max_steps) or False) for i in range(self.n)], {}

    def print_debug(self):
        print("---Link Debug---")
        for link in self.links:
            link.print_debug()
        print("---Sender Debug---")
        for sender in self.senders:
            sender.print_debug()

    def create_new_links_and_senders(self, random_link = False):

        # the defult links are not random
        if(random_link):
            bw    = random.uniform(self.min_bw, self.max_bw)
            lat   = random.uniform(self.min_lat, self.max_lat)
            queue = 1 + int(np.exp(random.uniform(self.min_queue, self.max_queue)))
            loss  = random.uniform(self.min_loss, self.max_loss)
        else:
            bw = 400.0
            lat = 0.03
            queue = 10.0
            loss = 0.00

        self.links = [Link(bw, lat, queue, loss), Link(bw, lat, queue, loss)]
        starting_rate = np.random.uniform(0.1, 0.2) * bw
        if starting_rate < (MIN_RATE):
            starting_rate = MIN_RATE

        self.senders = [Sender(starting_rate, [self.links[0], self.links[1]], 0, self.features,
                        history_len=self.history_len) for _ in range(self.n)]

        self.run_dur = 3 * lat

    def reset(self):

        self._add_episode_record()
        self._reset_parameters()

        self.net.reset()
        self.create_new_links_and_senders(random_link = RANDOM_LINK)

        self.net = Network(self.senders, self.links)

        if self.episodes_run > 0 and self.episodes_run % self.save_rate == 0:
            self._add_event_record()
        # self.dump_events_to_file("./dump-events" + self.log_dir +"/pcc_env_log_run_%d.json" % self.episodes_run)
        if self.episodes_run > 0 and self.episodes_run % self.save_rate == 0:
            print("dump episodes_run = %d" % self.episodes_run)
            self.dump_events_to_file("./dump-events" + self.log_dir +"/pcc_env_log_run_%d.json" % self.episodes_run, self.event_records)
            self.event_records = [[] for _ in range(self.n)]

        if self.episodes_run > 0 and self.episodes_run % 1000 == 0:
            print("dump steps at episode %d" % self.episodes_run)
            self.dump_events_to_file("./dump-events" + self.log_dir +"/steps_at_epi_%d.json" % self.episodes_run, self.step_records)
            self.step_records = [[] for _ in range(self.n)]

        self.episodes_run += 1

        self.net.run_for_dur(self.run_dur)
        self.net.run_for_dur(self.run_dur)

        for i in range(self.n):
            self.reward_ewmas[i] *= 0.99
            self.reward_ewmas[i] += 0.01 * self.reward_sums[i]
            self.reward_sums[i] = 0.0

        return self._get_all_sender_obs()

    def close(self):
        if self.viewer:
            self.viewer.close()
            self.viewer = None

    def dump_events_to_file(self, dirname, record):
        os.makedirs(os.path.dirname(dirname), exist_ok=True)
        with open(dirname, 'w') as f:
            json.dump(record, f, indent=4)


register(id='PccNs-v0', entry_point='network_sim:SimulatedNetworkEnv')
register(id='PccNs-v1', entry_point='network_sim:SimulatedMultAgentNetworkEnv')