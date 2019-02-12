import torch
import torch.autograd as autograd
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F


class CommNet(Agent):

    def __init__(self, args):
        '''
        args = {
        agent_num: int,
        hid_size: int,
        obs_size: int,
        continuous: bool,
        action_dim: int,
        comm_iters: int,
        action_heads_num: list(int)
        }
        '''
        super(CommNet, self).__init__()
        self.args = args
        self.construct_model()

    def construct_model(self):
        '''
        define the model of vanilla CommNet
        '''
        # encoder transforms observation to latent variables
        self.encoder = nn.Linear(self.args.obs_size, self.args.hid_size)
        # communication mask where the diagnal should be 0
        self.comm_mask = torch.ones(self.args.agent_num, self.args.agent_num) - torch.eye(self.args.agent_num, self.args.agent_num)
        # decoder transforms hidden states to action vector
        if self.args.continuous:
            self.action_mean = nn.Linear(self.args.hid_size, self.args.action_dim)
            self.action_log_std = nn.Parameter(torch.zeros(1, self.args.action_dim))
        else:
            self.action_heads = nn.ModuleList([nn.Linear(args.hid_size, o) for o in self.args.action_heads_num])
        # define communication inference
        self.f_module = nn.Linear(self.args.hid_size, self.args.hid_size)
        self.f_modules = nn.ModuleList([self.f_module for _ in range(self.args.comm_iters)])
        # define communication encoder
        self.C_module = nn.Linear(self.args.hid_size, self.args.hid_size)
        self.C_modules = nn.ModuleList([self.C_module for _ in range(self.args.comm_iters)])
        # initialise weights of communication encoder as 0
        for i in range(self.args.comm_iters):
            self.C_modules[i].weight.data.zero_()
        # define value function
        self.value_head = nn.Linear(self.hid_size, 1)

    def state_encoder(self, x):
        '''
        define a single forward pass of communication inference
        '''
        x = nn.Tanh(self.encoder(x))
        return x

    def get_agent_mask(self, batch_size, info):
        '''
        define the getter of agent mask to confirm the living agent
        '''
        n = self.args.agent_num
        if 'alive_mask' in info:
            agent_mask = torch.from_numpy(info['alive_mask'])
            num_agents_alive = agent_mask.sum()
        else:
            agent_mask = torch.ones(n)
            num_agents_alive = n
        # shape = (1, 1, n)
        agent_mask = agent_mask.view(1, 1, n)
        # shape = (batch_size, n ,n, 1)
        agent_mask = agent_mask.expand(batch_size, n, n).unsqueeze(-1)
        return num_agents_alive, agent_mask

    def action(self, obs, info={}):
        '''
        define the action process of vanilla CommNet
        '''
        # encode observation
        h = self.state_encoder(obs)
        # get the batch size
        batch_size = obs.size()[0]
        # get the total number of agents including dead
        n = self.args.agent_num
        # get the agent mask
        num_agents_alive, agent_mask = self.get_agent_mask(batch_size, info)
        # conduct the main process of communication
        for i in range(self.args.comm_iters):
            # shape = (batch_size, n, hid_size)->(batch_size, n, 1, hid_size)->(batch_size, n, n, hid_size)
            h = h.unsqueeze(-2).expand(-1, n, n, self.hid_size)
            # construct the communication mask
            mask = self.comm_mask.view(1, n, n) # shape = (1, n, n)
            mask = mask.expand(batch_size, n, n) # shape = (batch_size, n, n)
            mask = mask.unsqueeze(-1) # shape = (batch_size, n, n, 1)
            mask = mask.expand_as(h) # shape = (batch_size, n, n, hid_size)
            # mask each agent itself (collect the hidden state of other agents)
            h *= mask
            # mask the dead agent
            h *= agent_mask * agent_mask.transpose(1, 2)
            # average the hidden state
            h /= num_agents_alive - 1
            # calculate the communication vector
            c = h.sum(dim=1) # shape = (batch_size, n, hid_size)
            # h_{j}^{i+1} = \sigma(H_j * h_j^{i+1} + C_j * c_j^{i+1})
            h = nn.Tanh(sum([self.f_modules[i](h), self.C_modules[i](c)]))
        # calculate the value function (critic)
        value_head = self.value_head(h)
        # calculate the action vector (actor)
        if self.continuous:
            # shape = (batch_size, n, action_dim)
            action_mean = self.action_mean(h)
            action_log_std = self.action_log_std.expand_as(action_mean)
            action_std = torch.exp(action_log_std)
            # will be used later to sample
            action = (action_mean, action_log_std, action_std)
        else:
            # discrete actions, shape = (batch_size, n, action_type, action_num)
            action = [F.log_softmax(head(h), dim=-1) for head in self.action_heads]
        return action, value_head
