#!/usr/bin/env python
# encoding: utf-8
# File Name: gcn.py
# Author: Jiezhong Qiu
# Create Time: 2019/12/13 15:38
# TODO:

import dgl
import dgl.function as fn
import torch
import torch.nn as nn
import torch.nn.functional as F
# from dgl.model_zoo.chem.gnn import GCNLayer
from dgl.nn.pytorch import AvgPooling, Set2Set

from dgl.nn.pytorch import GraphConv, GATConv
from ...model import TorchModel

class GCNLayer(nn.Module):
    """Single layer GCN for updating node features
    Parameters
    ----------
    in_feats : int
        Number of input atom features
    out_feats : int
        Number of output atom features
    activation : activation function
        Default to be ReLU
    residual : bool
        Whether to use residual connection, default to be True
    batchnorm : bool
        Whether to use batch normalization on the output,
        default to be True
    dropout : float
        The probability for dropout. Default to be 0., i.e. no
        dropout is performed.
    """
    def __init__(self, in_feats, out_feats, activation=F.relu,
                 residual=True, batchnorm=True, dropout=0.):
        super(GCNLayer, self).__init__()

        self.activation = activation
        self.graph_conv = GraphConv(in_feats=in_feats, out_feats=out_feats,
                                    norm="none", activation=activation)
        self.dropout = nn.Dropout(dropout)

        self.residual = residual
        if residual:
            self.res_connection = nn.Linear(in_feats, out_feats)

        self.bn = batchnorm
        if batchnorm:
            self.bn_layer = nn.BatchNorm1d(out_feats)

    def forward(self, g, feats):
        """Update atom representations
        Parameters
        ----------
        g : DGLGraph
            DGLGraph with batch size B for processing multiple molecules in parallel
        feats : FloatTensor of shape (N, M1)
            * N is the total number of atoms in the batched graph
            * M1 is the input atom feature size, must match in_feats in initialization
        Returns
        -------
        new_feats : FloatTensor of shape (N, M2)
            * M2 is the output atom feature size, must match out_feats in initialization
        """
        new_feats = self.graph_conv(g, feats)
        if self.residual:
            res_feats = self.activation(self.res_connection(feats))
            new_feats = new_feats + res_feats
        new_feats = self.dropout(new_feats)

        if self.bn:
            new_feats = self.bn_layer(new_feats)

        return new_feats

class GATLayer(nn.Module):
    """Single layer GAT for updating node features
    Parameters
    ----------
    in_feats : int
        Number of input atom features
    out_feats : int
        Number of output atom features for each attention head
    num_heads : int
        Number of attention heads
    feat_drop : float
        Dropout applied to the input features
    attn_drop : float
        Dropout applied to attention values of edges
    alpha : float
        Hyperparameter in LeakyReLU, slope for negative values. Default to be 0.2
    residual : bool
        Whether to perform skip connection, default to be False
    agg_mode : str
        The way to aggregate multi-head attention results, can be either
        'flatten' for concatenating all head results or 'mean' for averaging
        all head results
    activation : activation function or None
        Activation function applied to aggregated multi-head results, default to be None.
    """
    def __init__(self, in_feats, out_feats, num_heads, feat_drop, attn_drop,
                 alpha=0.2, residual=True, agg_mode='flatten', activation=None):
        super(GATLayer, self).__init__()
        self.gnn = GATConv(in_feats=in_feats, out_feats=out_feats, num_heads=num_heads,
                           feat_drop=feat_drop, attn_drop=attn_drop,
                           negative_slope=alpha, residual=residual)
        assert agg_mode in ['flatten', 'mean']
        self.agg_mode = agg_mode
        self.activation = activation

    def forward(self, bg, feats):
        """Update atom representations
        Parameters
        ----------
        bg : DGLGraph
            Batched DGLGraphs for processing multiple molecules in parallel
        feats : FloatTensor of shape (N, M1)
            * N is the total number of atoms in the batched graph
            * M1 is the input atom feature size, must match in_feats in initialization
        Returns
        -------
        new_feats : FloatTensor of shape (N, M2)
            * M2 is the output atom feature size. If self.agg_mode == 'flatten', this would
              be out_feats * num_heads, else it would be just out_feats.
        """
        new_feats = self.gnn(bg, feats)
        if self.agg_mode == 'flatten':
            new_feats = new_feats.flatten(1)
        else:
            new_feats = new_feats.mean(1)

        if self.activation is not None:
            new_feats = self.activation(new_feats)

        return new_feats

@TorchModel.register("UnsupervisedGCN", "PyTorch")
class UnsupervisedGCN(nn.Module):
    def __init__(
        self,
        hidden_size=64,
        num_layer=2,
        readout="avg",
        layernorm: bool = False,
        set2set_lstm_layer: int = 3,
        set2set_iter: int = 6,
    ):
        super(UnsupervisedGCN, self).__init__()
        self.layers = nn.ModuleList(
            [
                GCNLayer(
                    in_feats=hidden_size,
                    out_feats=hidden_size,
                    activation=F.relu if i + 1 < num_layer else None,
                    residual=False,
                    batchnorm=False,
                    dropout=0.0,
                )
                for i in range(num_layer)
            ]
        )
        if readout == "avg":
            self.readout = AvgPooling()
        elif readout == "set2set":
            self.readout = Set2Set(
                hidden_size, n_iters=set2set_iter, n_layers=set2set_lstm_layer
            )
            self.linear = nn.Linear(2 * hidden_size, hidden_size)
        elif readout == "root":
            # HACK: process outside the model part
            self.readout = lambda _, x: x
        else:
            raise NotImplementedError
        self.layernorm = layernorm
        if layernorm:
            self.ln = nn.LayerNorm(hidden_size, elementwise_affine=False)
            # self.ln = nn.BatchNorm1d(hidden_size, affine=False)

    def forward(self, g, feats, efeats=None):
        for layer in self.layers:
            feats = layer(g, feats)
        feats = self.readout(g, feats)
        if isinstance(self.readout, Set2Set):
            feats = self.linear(feats)
        if self.layernorm:
            feats = self.ln(feats)
        return feats


if __name__ == "__main__":
    model = UnsupervisedGCN()
    print(model)
    g = dgl.DGLGraph()
    g.add_nodes(3)
    g.add_edges([0, 0, 1], [1, 2, 2])
    feat = torch.rand(3, 64)
    print(model(g, feat).shape)

