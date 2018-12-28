import torch
import numpy as np
from torch import nn

from qiqc.builder import build_aggregator
from qiqc.models import Word2VecEx
from qiqc.models import WordEmbedding
from qiqc.models import BinaryClassifier


def build_sampler(i, epoch, weights):
    sampler = None
    # if epoch % 2 == 0:
    #     sampler = torch.utils.data.WeightedRandomSampler(
    #         weights=weights, num_samples=len(weights), replacement=True)
    # else:
    #     sampler = None
    return sampler


def build_embedding(
        i, config, tokens, word_freq, token2id, pretrained_vectors):
    assert isinstance(pretrained_vectors, np.ndarray)
    # Fine tuning embedding
    model = Word2VecEx(**config['embedding']['params'])
    model.build_vocab_from_freq(word_freq)
    model.wv.vectors[:] = pretrained_vectors
    if config['embedding']['finetune']:
        model.train(tokens, total_examples=len(tokens), epochs=1)
        model.wv.vectors[(pretrained_vectors == 0).all(axis=1)] = 0
    mat = model.build_embedding_matrix(
        token2id, standardize=config['embedding']['standardize'])
    if config['embedding']['add_los']:
        mean = mat[(mat != 0).all(axis=1)].mean(axis=0)
        std = mat[(mat != 0).all(axis=1)].std(axis=0)
        vec = np.random.normal(mean, std, (config['maxlen'], mat.shape[1]))
        mat[1:config['maxlen'] + 1] = vec

    return mat


class Encoder(nn.Module):

    def __init__(self, config, embedding_matrix):
        super().__init__()
        self.embed = WordEmbedding(
            *embedding_matrix.shape,
            n_hidden=config['embed']['n_hidden'],
            freeze_embed=config['embed']['freeze_embed'],
            pretrained_vectors=embedding_matrix,
            position=config['embed']['position'],
            hidden_bn=config['embed']['hidden_bn'],
            dropout=config['embed']['dropout'],
        )
        self.encoder = nn.LSTM(
            input_size=self.embed.out_dim,
            hidden_size=config['encoder']['n_hidden'],
            num_layers=config['encoder']['n_layers'],
            dropout=config['encoder']['dropout'],
            bidirectional=True,
            batch_first=True,
        )
        self.aggregator = build_aggregator(
            config['encoder']['aggregator'],
        )

    def forward(self, X, mask):
        h = self.embed(X)
        h, _ = self.encoder(h)
        h = self.aggregator(h, mask)
        return h


def build_model(i, config, embedding_matrix):
    encoder = Encoder(config['model'], embedding_matrix)
    clf = BinaryClassifier(config['model'], encoder)
    return clf


def build_optimizer(i, config, model):
    optimizer = torch.optim.Adam(
        model.parameters(), lr=float(config['optimizer']['lr']))
    # optimizer = torch.optim.SGD(
    #     model.parameters(), lr=float(config['optimizer']['lr']))
    return optimizer
