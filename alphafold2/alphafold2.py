import torch
from torch import nn, einsum
from functools import partial
import torch.nn.functional as F

from einops import rearrange

# constants

NUM_AMINO_ACIDS = 21
DISTOGRAM_BUCKETS = 37

# helpers

def exists(val):
    return val is not None

# helper classes

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn

    def forward(self, x, **kwargs):
        x = self.norm(x)
        return self.fn(x, **kwargs)

class GEGLU(nn.Module):
    def forward(self, x):
        x, gates = x.chunk(2, dim = -1)
        return x * F.gelu(gates)

class FeedForward(nn.Module):
    def __init__(
        self,
        dim,
        mult = 4,
        dropout = 0.
    ):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * mult * 2),
            GEGLU(),
            nn.Dropout(dropout),
            nn.Linear(dim * mult, dim)
        )

    def forward(self, x):
        return self.net(x)

# attention

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        heads = 8,
        dim_head = 64,
        dropout = 0.
    ):
        super().__init__()
        inner_dim = dim_head * heads
        self.heads= heads
        self.scale = dim_head ** -0.5
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)
        self.to_out = nn.Linear(inner_dim, dim)

        self.dropout = nn.Dropout(dropout)

    def forward(self, x, context = None, mask = None, context_mask = None):
        h = self.heads

        qkv = self.to_qkv(x)
        q, k, v = rearrange(qkv, 'b n (h qkv d) -> b h n qkv d', h = h, qkv = 3).unbind(dim = -2)
        dots = einsum('b h i d, b h j d -> b h i j', q, k) * self.scale

        if exists(mask):
            mask_value = -torch.finfo(dots.dtype).max
            mask = mask[:, None, :, None] * mask[:, None, None, :]
            dots.masked_fill_(~mask, mask_value)

        attn = dots.softmax(dim = -1)
        attn = self.dropout(attn)

        out = einsum('b h i j, b h j d -> b h i d', attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

class AxialAttention(nn.Module):
    def __init__(
        self,
        **kwargs
    ):
        super().__init__()
        self.attn_width = Attention(**kwargs)
        self.attn_height = Attention(**kwargs)

    def forward(self, x, context = None, mask = None, context_mask = None):
        b, h, w, d = x.shape

        w_x = rearrange(x, 'b h w d -> (b w) h d')
        w_out = self.attn_width(w_x, mask = mask)
        w_out = rearrange(w_out, '(b w) h d -> b h w d', h = h, w = w)

        h_x = rearrange(x, 'b h w d -> (b h) w d')
        h_out = self.attn_height(h_x, mask = mask)
        h_out = rearrange(h_out, '(b h) w d -> b h w d', h = h, w = w)

        return w_out + h_out

# main class

class Alphafold2(nn.Module):
    def __init__(
        self,
        *,
        dim,
        max_seq_len = 2048,
        depth = 6,
        heads = 8,
        dim_head = 64,
        num_tokens = 21,
        attn_dropout = 0.,
        ff_dropout = 0.
    ):
        super().__init__()
        self.token_emb = nn.Embedding(NUM_AMINO_ACIDS, dim)
        self.pos_emb = nn.Embedding(max_seq_len, dim)

        wrapper = partial(PreNorm, dim)

        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                wrapper(AxialAttention(dim = dim, heads = heads, dim_head = dim_head, dropout = attn_dropout)),
                wrapper(AxialAttention(dim = dim, heads = heads, dim_head = dim_head, dropout = attn_dropout)),
                wrapper(FeedForward(dim = dim, dropout = ff_dropout)),
            ]))


        self.msa_layers = nn.ModuleList([])
        for _ in range(depth):
            self.msa_layers.append(nn.ModuleList([
                wrapper(Attention(dim = dim, heads = heads, dim_head = dim_head, dropout = attn_dropout)),
                wrapper(Attention(dim = dim, heads = heads, dim_head = dim_head, dropout = attn_dropout)),
                wrapper(FeedForward(dim = dim, dropout = ff_dropout))
            ]))

        self.norm = nn.LayerNorm(dim)
        self.to_distogram_logits = nn.Linear(dim, DISTOGRAM_BUCKETS)

    def forward(self, seq, msa, mask = None, msa_mask = None):
        device, msa_shape = seq.device, msa.shape

        # embed main sequence

        x = self.token_emb(seq)
        x += self.pos_emb(torch.arange(seq.shape[1], device = device))[None, ...]
        x = x[:, :, None, :] + x[:, None, :, :] # create pair-wise residue embeds

        # embed multiple sequence alignment

        m = self.token_emb(msa)
        m += self.pos_emb(torch.arange(msa.shape[-1], device = device))[None, None, ...]
        m = rearrange(m, 'b m n d -> b (m n) d')

        if exists(msa_mask):
            msa_mask = rearrange(msa_mask, 'b m n -> b (m n)')

        # trunk

        for ((attn, cross_attn, ff), (msa_attn, msa_cross_attn, msa_ff)) in zip(self.layers, self.msa_layers):
            x = attn(x, mask = mask) + x
            m = msa_attn(m, mask = msa_mask) + m

            x = ff(x) + x
            m = ff(m) + m

        x = self.norm(x)
        return self.to_distogram_logits(x)
