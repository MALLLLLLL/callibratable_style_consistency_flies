import torch
import torch.nn as nn
import math

from util.logging import LogEntry
from lib.distributions import Normal


class BaseSequentialModel(nn.Module):

    model_args = []
    requires_labels = False
    requires_environment = False
    is_recurrent = True

    def __init__(self, model_config):
        super().__init__()

        if 'recurrent' in model_config:
            self.is_recurrent = model_config['recurrent']

        # Assert rnn_dim and num_layers are defined if model is recurrent
        if self.is_recurrent:
            if 'rnn_dim' not in self.model_args:
                self.model_args.append('rnn_dim') # TODO make distinction between enc_rnn_dim and dec_rnn_dim
            if 'num_layers' not in self.model_args:
                self.model_args.append('num_layers')

        # Assert label_dim is defined if model requires labels
        if self.requires_labels and 'label_dim' not in self.model_args:
            # print("uhoh adding labels here")
            self.model_args.append('label_dim')

        # Check for missing arguments
        missing_args = set(self.model_args) - set(model_config)
        assert len(missing_args) == 0, 'model_config is missing these arguments:\n\t{}'.format(', '.join(missing_args))

        self.config = model_config
        self.log = LogEntry()
        self.stage = 0 # some models have multi-stage training

        self._construct_model()
        self._define_losses()

        # TODO remove this
        if self.is_recurrent:
            assert hasattr(self, 'dec_rnn')

    def _construct_model(self):
        raise NotImplementedError

    def _define_losses(self):
        raise NotImplementedError

    @property
    def num_parameters(self):
        if not hasattr(self, '_num_parameters'):
            self._num_parameters = 0
            for p in self.parameters():
                count = 1
                for s in p.size():
                    count *= s
                self._num_parameters += count

        return self._num_parameters

    def init_hidden_state(self, batch_size=1):
        #TODO this will be updated/removed
        return torch.zeros(self.config['num_layers'], batch_size, self.config['rnn_dim'])

    def update_hidden(self, state, action):
        assert self.is_recurrent
        state_action_pair = torch.cat([state, action], dim=1).unsqueeze(0)
        _, self.hidden = self.dec_rnn(state_action_pair, self.hidden)

    # TODO this should be in TVAEmodel
    def encode(self, states, actions=None, labels=None):
        enc_birnn_input = states
        # print("----states-----")
        # print(enc_birnn_input)
        # print("---------------")
        if actions is not None:
            assert states.size(0) == actions.size(0)
            enc_birnn_input = torch.cat([states, actions], dim=-1)


        # print("---OG INPUT------")
        # print(enc_birnn_input)
        # print("-----------------")

        hiddens, _ = self.enc_birnn(enc_birnn_input)
        # print("-----hiddens------")
        # print(hiddens)
        # print("-------------------")

        # import numpy as np
        # if np.sum(np.isnan(hiddens.detach().numpy())):
        #     for name, param in self.enc_birnn.named_parameters():
        #         print(name)
        #         print(np.sum(np.isnan(param.detach().numpy())))
        #     import pdb; pdb.set_trace()

        # print("-------avg_hiddens-----")
        avg_hiddens = torch.mean(hiddens, dim=0)
        # print("-----------------------")

        enc_fc_input = avg_hiddens
        if labels is not None:
            # print("IN MODEL HERE")
            # print(avg_hiddens.shape)
            # print("BOOM")
            enc_fc_input = torch.cat([avg_hiddens, labels], 1)
            # print(enc_fc_input.shape)
            # print(self.enc_fc)
        # print("-----input-----")
        # print(enc_fc_input)
        # print("---------------")
        enc_h = self.enc_fc(enc_fc_input) if hasattr(self, 'enc_fc') else enc_fc_input
        # print("------output------")
        # print(enc_h)
        # print("------------------")
        enc_mean = self.enc_mean(enc_h)
        enc_logvar = self.enc_logvar(enc_h)

        # print(enc_mean)
        # print(enc_logvar)

        return Normal(enc_mean, enc_logvar)

    # TODO this should be in TVAEmodel
    def decode_action(self, state, actions=None):
        dec_fc_input = torch.cat([state, self.z], dim=1)
        if self.requires_labels:
            dec_fc_input = torch.cat([dec_fc_input, self.labels], dim=1)
        if self.is_recurrent:
            dec_fc_input = torch.cat([dec_fc_input, self.hidden[-1]], dim=1)
        if actions is not None:
            dec_fc_input = torch.cat([dec_fc_input, actions], dim=1)

        dec_h = self.dec_action_fc(dec_fc_input)
        dec_mean = self.dec_action_mean(dec_h)

        if isinstance(self.dec_action_logvar, nn.Parameter):
            dec_logvar = self.dec_action_logvar
        else:
            dec_logvar = self.dec_action_logvar(dec_h)
        return Normal(dec_mean, dec_logvar)

    # TODO this goes to Policy level
    def reset_policy(self, labels=None, z=None, temperature=1.0, num_samples=0, device=None):
        if self.requires_labels:
            assert labels is not None
            assert labels.size(1) == self.config['label_dim']

            if z is not None:
                assert labels.size(0) == z.size(0)

            num_samples = labels.size(0)
            device = labels.device
            self.labels = labels

        if z is None:
            # TODO doesnt work for tvaep
            assert num_samples > 0
            assert device is not None
            z = torch.randn(num_samples, self.config['z_dim']).to(device)

        self.z = z
        self.temperature = temperature

        if self.is_recurrent:
            self.hidden = self.init_hidden_state(batch_size=z.size(0)).to(z.device)

    # TODO this goes to Policy level
    def act(self, state, action=None, sample=True):
        if "conditional_single_fly_policy_2_to_2" in self.config and self.config["conditional_single_fly_policy_2_to_2"]:
            assert(action is not None)
            action_likelihood = self.decode_action(state, actions=action)
        else:
            action_likelihood = self.decode_action(state)

        action = action_likelihood.sample(temperature=self.temperature) if sample else action_likelihood.mean

        if self.is_recurrent:
            self.update_hidden(state, action)

        return action

    # TODO optimization should be move to the Agent level
    def prepare_stage(self, train_config):
        self.stage += 1
        self.init_optimizer(train_config['learning_rate']) # TODO learning rates for different stages
        self.clip_grad = lambda : nn.utils.clip_grad_norm_(self.parameters(), train_config['clip'])

    def init_optimizer(self, lr, l2_penalty=0.0):
        self.optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, self.parameters()),
            lr=lr, weight_decay=l2_penalty)

    def optimize(self, losses):
        assert isinstance(losses, dict)
        self.optimizer.zero_grad()
        total_loss = sum(losses.values())
        # nn.utils.clip_grad_norm_(self.parameters(), 0.5)
        total_loss.backward()
        self.clip_grad()
        self.optimizer.step()
        # TODO don't step is nans

    def filter_and_load_state_dict(self, state_dict):
        """
        Temporary fix to be compatible with previous model naming
        TODO should rename and save once
        """
        for _ in range(len(state_dict)):
            name, param = state_dict.popitem(False) # pop first item
            state_dict[name.replace('transition', 'dynamics')] = param

        self.load_state_dict(state_dict)
