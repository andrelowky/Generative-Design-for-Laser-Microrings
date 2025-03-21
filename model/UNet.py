import math
import torch
from torch import nn
from torch.nn import init
from torch.nn import functional as F
from abc import ABC, abstractmethod


# use sinusoidal position embedding to encode time step (https://arxiv.org/abs/1706.03762)
def timestep_embedding(timesteps, dim, max_period=10000):
	"""
	Create sinusoidal timestep embeddings.
	:param timesteps: a 1-D Tensor of N indices, one per batch element.
					  These may be fractional.
	:param dim: the dimension of the output.
	:param max_period: controls the minimum frequency of the embeddings.
	:return: an [N x dim] Tensor of positional embeddings.
	"""
	half = dim // 2
	freqs = torch.exp(
		-math.log(max_period) * torch.arange(start=0, end=half, dtype=torch.float32) / half
	).to(device=timesteps.device)
	args = timesteps[:, None].float() * freqs[None]
	embedding = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
	if dim % 2:
		embedding = torch.cat([embedding, torch.zeros_like(embedding[:, :1])], dim=-1)
	return embedding


# define TimestepEmbedSequential to support `time_emb` as extra input
class TimestepBlock(nn.Module):
    """
    Any module where forward() takes timestep embeddings as a second argument.
    """

    @abstractmethod
    def forward(self, x, emb):
        """
        Apply the module to `x` given `emb` timestep embeddings.
        """


class TimestepEmbedSequential(nn.Sequential, TimestepBlock):
    """
    A sequential module that passes timestep embeddings to the children that
    support it as an extra input.
    """

    def forward(self, x, emb, params=None):
        for layer in self:
            if isinstance(layer, CrossModalAttentionBlock) and params is not None:
                x = layer(x, params)
            elif isinstance(layer, TimestepBlock):
                x = layer(x, emb)
            else:
                x = layer(x)
        return x


# use GN for norm layer
def norm_layer(channels):
    return nn.GroupNorm(32, channels)


# Residual block
class ResidualBlock(TimestepBlock):
    def __init__(self, in_channels, out_channels, time_channels, dropout):
        super().__init__()
        self.conv1 = nn.Sequential(
            norm_layer(in_channels),
            nn.SiLU(),
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1)
        )

        # projection for time step embedding
        self.time_emb = nn.Sequential(
            nn.SiLU(),
            nn.Linear(time_channels, out_channels)
        )

        self.conv2 = nn.Sequential(
            norm_layer(out_channels),
            nn.SiLU(),
            nn.Dropout(p=dropout),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1)
        )

        if in_channels != out_channels:
            self.shortcut = nn.Conv2d(in_channels, out_channels, kernel_size=1)
        else:
            self.shortcut = nn.Identity()

    def forward(self, x, t):
        """
        `x` has shape `[batch_size, in_dim, height, width]`
        `t` has shape `[batch_size, time_dim]`
        """
        h = self.conv1(x)
        # Add time step embeddings
        h += self.time_emb(t)[:, :, None, None]
        h = self.conv2(h)
        return h + self.shortcut(x)


# Attention block with shortcut
class AttentionBlock(nn.Module):
    def __init__(self, channels, num_heads=1):
        super().__init__()
        self.num_heads = num_heads
        assert channels % num_heads == 0

        self.norm = norm_layer(channels)
        self.qkv = nn.Conv2d(channels, channels * 3, kernel_size=1, bias=False)
        self.proj = nn.Conv2d(channels, channels, kernel_size=1)

    def forward(self, x):
        B, C, H, W = x.shape
        qkv = self.qkv(self.norm(x))
        q, k, v = qkv.reshape(B * self.num_heads, -1, H * W).chunk(3, dim=1)
        scale = 1. / math.sqrt(math.sqrt(C // self.num_heads))
        attn = torch.einsum("bct,bcs->bts", q * scale, k * scale)
        attn = attn.softmax(dim=-1)
        h = torch.einsum("bts,bcs->bct", attn, v)
        h = h.reshape(B, -1, H, W)
        h = self.proj(h)
        return h + x


# Cross-Modal Attention Block for joint image-parameter diffusion
class CrossModalAttentionBlock(nn.Module):
    def __init__(self, img_channels, param_dim, num_heads=1):
        super().__init__()
        self.num_heads = num_heads
        assert img_channels % num_heads == 0
        
        # Image feature processing
        self.img_norm = norm_layer(img_channels)
        self.img_to_q = nn.Conv2d(img_channels, img_channels, kernel_size=1, bias=False)
        
        # Parameter processing
        self.param_norm = nn.LayerNorm(param_dim)
        self.param_to_kv = nn.Linear(param_dim, img_channels * 2)
        
        # Output projection
        self.proj = nn.Conv2d(img_channels, img_channels, kernel_size=1)
        
    def forward(self, img, params):
        B, C, H, W = img.shape
        
        # Process image to create queries
        img_norm = self.img_norm(img)
        q = self.img_to_q(img_norm)
        q = q.reshape(B * self.num_heads, C // self.num_heads, H * W)  # [B*nh, C/nh, H*W]
        
        # Process parameters to create keys and values
        param_norm = self.param_norm(params)
        kv = self.param_to_kv(param_norm)  # [B, img_channels*2]
        k, v = kv.chunk(2, dim=1)
        
        # Reshape k, v for attention
        k = k.unsqueeze(-1)  # [B, C, 1]
        v = v.unsqueeze(-1)  # [B, C, 1]
        
        # Reshape for multi-head attention
        k = k.reshape(B * self.num_heads, C // self.num_heads, 1)  # [B*nh, C/nh, 1]
        v = v.reshape(B * self.num_heads, C // self.num_heads, 1)  # [B*nh, C/nh, 1]
        
        # Cross-attention
        scale = 1. / math.sqrt(math.sqrt(C // self.num_heads))
        attn = torch.einsum("bct,bcs->bts", q * scale, k * scale)  # [B*nh, H*W, 1]
        attn = attn.softmax(dim=1)
        
        # Apply attention weights
        h = torch.einsum("bts,bcs->bct", attn, v)  # [B*nh, C/nh, H*W]
        h = h.reshape(B, C, H, W)
        h = self.proj(h)
        
        return h + img  # Residual connection


# Parameter Encoder MLP
class ParameterEncoder(nn.Module):
    def __init__(self, param_dim, hidden_dim, out_dim, time_dim):
        super().__init__()
        self.time_embed = nn.Sequential(
            nn.Linear(time_dim, hidden_dim),
            nn.SiLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )
        
        self.input_proj = nn.Linear(param_dim, hidden_dim)
        
        self.layers = nn.ModuleList([
            nn.Sequential(
                nn.Linear(hidden_dim, hidden_dim),
                nn.SiLU(),
                nn.Linear(hidden_dim, hidden_dim)
            )
            for _ in range(2)  # 3 layers of transformation
        ])
        
        self.out_proj = nn.Linear(hidden_dim, out_dim)
        
    def forward(self, params, time_emb):
        # Embed parameters
        h = self.input_proj(params)
        
        # Add time embedding
        time_emb = self.time_embed(time_emb)
        h = h + time_emb
        
        # Process through layers
        for layer in self.layers:
            h = layer(h) + h  # Residual connection
            
        return self.out_proj(h)


# upsample
class Upsample(nn.Module):
    def __init__(self, channels, use_conv):
        super().__init__()
        self.use_conv = use_conv
        if use_conv:
            self.conv = nn.Conv2d(channels, channels, kernel_size=3, padding=1)

    def forward(self, x):
        x = F.interpolate(x, scale_factor=2, mode="nearest")
        if self.use_conv:
            x = self.conv(x)
        return x


# downsample
class Downsample(nn.Module):
    def __init__(self, channels, use_conv):
        super().__init__()
        self.use_conv = use_conv
        if use_conv:
            self.op = nn.Conv2d(channels, channels, kernel_size=3, stride=2, padding=1)
        else:
            self.op = nn.AvgPool2d(stride=2, kernel_size=2)

    def forward(self, x):
        return self.op(x)


# The modified UNet model with cross-attention for joint diffusion
class UNet(nn.Module):
	def __init__(
			self,
			in_channels=1,
			out_channels=1,
			model_channels=128,
			param_dim=8,
			param_hidden_dim=128,
			num_res_blocks=2,
			attention_resolutions=(8, 16),
			dropout=0.1,
			channel_mult=(1, 2, 2, 2),
			conv_resample=True,
			num_heads=4,
			use_cross_attention=True
	):
		super().__init__()
	
		self.in_channels = in_channels
		self.out_channels = out_channels
		self.model_channels = model_channels
		self.param_dim = param_dim
		self.num_res_blocks = num_res_blocks
		self.attention_resolutions = attention_resolutions
		self.dropout = dropout
		self.channel_mult = channel_mult
		self.conv_resample = conv_resample
		self.num_heads = num_heads
		self.use_cross_attention = use_cross_attention
	
		# time embedding
		time_embed_dim = model_channels * 4
		self.time_embed = nn.Sequential(
			nn.Linear(model_channels, time_embed_dim),
			nn.SiLU(),
			nn.Linear(time_embed_dim, time_embed_dim),
		)
	
		# Parameter encoder
		self.param_encoder = ParameterEncoder(
			param_dim=param_dim,
			hidden_dim=param_hidden_dim,
			out_dim=param_hidden_dim,
			time_dim=time_embed_dim
		)
		
		#### encoder-decoder image+parameter path ####
		
		# down blocks
		self.down_blocks = nn.ModuleList([
			TimestepEmbedSequential(nn.Conv2d(in_channels, model_channels, kernel_size=3, padding=1))
		])
		down_block_chans = [model_channels]
		ch = model_channels
		ds = 1
		for level, mult in enumerate(channel_mult):
			for _ in range(num_res_blocks):
				layers = [
					ResidualBlock(ch, mult * model_channels, time_embed_dim, dropout)
				]
				ch = mult * model_channels
				if ds in attention_resolutions: # attention kicks in at specific resolutions in the downsampling
					# Add self-attention to images
					layers.append(AttentionBlock(ch, num_heads=num_heads))
					
					# Add cross-attention between image and parameter modalities
					if self.use_cross_attention:
						layers.append(CrossModalAttentionBlock(ch, param_hidden_dim, num_heads=num_heads))
						
				self.down_blocks.append(TimestepEmbedSequential(*layers))
				down_block_chans.append(ch)
			if level != len(channel_mult) - 1:  # don't use downsample for the last stage
				self.down_blocks.append(TimestepEmbedSequential(Downsample(ch, conv_resample)))
				down_block_chans.append(ch)
				ds *= 2
	
		# middle block
		middle_layers = [
			ResidualBlock(ch, ch, time_embed_dim, dropout),
			AttentionBlock(ch, num_heads=num_heads)
		]
		
		# Add a cross-attention block at the bottleneck
		if self.use_cross_attention:
			middle_layers.append(CrossModalAttentionBlock(ch, param_hidden_dim, num_heads=num_heads))
			
		middle_layers.append(ResidualBlock(ch, ch, time_embed_dim, dropout))
		
		self.middle_block = TimestepEmbedSequential(*middle_layers)
	
		# up blocks
		self.up_blocks = nn.ModuleList([])
		for level, mult in list(enumerate(channel_mult))[::-1]:
			for i in range(num_res_blocks + 1):
				layers = [
					ResidualBlock(
						ch + down_block_chans.pop(),
						model_channels * mult,
						time_embed_dim,
						dropout
					)
				]
				ch = model_channels * mult
				if ds in attention_resolutions: # attention kicks in at specific resolutions in the upsampling
					# Add self-attention to images
					layers.append(AttentionBlock(ch, num_heads=num_heads))
					
					# Add cross-attention between image and parameter modalities
					if self.use_cross_attention:
						layers.append(CrossModalAttentionBlock(ch, param_hidden_dim, num_heads=num_heads))
						
				if level and i == num_res_blocks:
					layers.append(Upsample(ch, conv_resample))
					ds //= 2
				self.up_blocks.append(TimestepEmbedSequential(*layers))
	
		#### encoder-decoder image+parameter path ####
	
		# Output projection for image
		self.img_out = nn.Sequential(
			norm_layer(ch),
			nn.SiLU(),
			nn.Conv2d(model_channels, out_channels, kernel_size=3, padding=1),
		)
	
		# Parameter output projector
		self.param_out = nn.Linear(param_hidden_dim, param_dim)
	
	def forward(self, x, params, timesteps):
		"""
		Apply the model to an input batch.
		:param x: an [N x C x H x W] Tensor of image inputs.
		:param params: an [N x param_dim] Tensor of parameter inputs.
		:param timesteps: a 1-D batch of timesteps.
		:return: tuple of:
				- image noise prediction [N x C x H x W]
				- parameter noise prediction [N x param_dim]
		"""
		hs = []
		# Time step embedding
		emb = self.time_embed(timestep_embedding(timesteps, self.model_channels))
		
		# Process parameters
		param_features = self.param_encoder(params, emb)
		
		# Down stage for image
		h = x
		for module in self.down_blocks:
			h = module(h, emb, param_features)
			hs.append(h)
			
		# Middle stage
		h = self.middle_block(h, emb, param_features)
		
		# Up stage
		for module in self.up_blocks:
			cat_in = torch.cat([h, hs.pop()], dim=1)
			h = module(cat_in, emb, param_features)
			
		# Output projections
		img_out = self.img_out(h)
		param_out = self.param_out(param_features)
		
		return img_out, param_out


# This one is the half model for predicting properties, used for energy guidance
class UNet_Energy(nn.Module):
	def __init__(
			self,
			in_channels=1,
			out_channels=1,
			model_channels=128,
			param_dim=8,
			param_hidden_dim=128,
			prop_dim=1,
			prop_hidden_dim=128,
			num_res_blocks=2,
			attention_resolutions=(8, 16),
			dropout=0.1,
			channel_mult=(1, 2, 2, 2),
			conv_resample=True,
			num_heads=4,
			use_cross_attention=True
	):
		super().__init__()
	
		self.in_channels = in_channels
		self.out_channels = out_channels
		self.model_channels = model_channels
		self.param_dim = param_dim
		self.prop_dim = prop_dim
		self.num_res_blocks = num_res_blocks
		self.attention_resolutions = attention_resolutions
		self.dropout = dropout
		self.channel_mult = channel_mult
		self.conv_resample = conv_resample
		self.num_heads = num_heads
		self.use_cross_attention = use_cross_attention
	
		# time embedding
		time_embed_dim = model_channels * 4
		self.time_embed = nn.Sequential(
			nn.Linear(model_channels, time_embed_dim),
			nn.SiLU(),
			nn.Linear(time_embed_dim, time_embed_dim),
		)
	
		# Parameter encoder
		self.param_encoder = ParameterEncoder(
			param_dim=param_dim,
			hidden_dim=param_hidden_dim,
			out_dim=param_hidden_dim,
			time_dim=time_embed_dim
		)
		
		#### encoder-decoder image+parameter path ####
		
		# down blocks
		self.down_blocks = nn.ModuleList([
			TimestepEmbedSequential(nn.Conv2d(in_channels, model_channels, kernel_size=3, padding=1))
		])
		down_block_chans = [model_channels]
		ch = model_channels
		ds = 1
		for level, mult in enumerate(channel_mult):
			for _ in range(num_res_blocks):
				layers = [
					ResidualBlock(ch, mult * model_channels, time_embed_dim, dropout)
				]
				ch = mult * model_channels
				if ds in attention_resolutions: # attention kicks in at specific resolutions in the downsampling
					# Add self-attention to images
					layers.append(AttentionBlock(ch, num_heads=num_heads))
					
					# Add cross-attention between image and parameter modalities
					if self.use_cross_attention:
						layers.append(CrossModalAttentionBlock(ch, param_hidden_dim, num_heads=num_heads))
						
				self.down_blocks.append(TimestepEmbedSequential(*layers))
				down_block_chans.append(ch)
			if level != len(channel_mult) - 1:  # don't use downsample for the last stage
				self.down_blocks.append(TimestepEmbedSequential(Downsample(ch, conv_resample)))
				down_block_chans.append(ch)
				ds *= 2
	
		# middle block
		middle_layers = [
			ResidualBlock(ch, ch, time_embed_dim, dropout),
			AttentionBlock(ch, num_heads=num_heads)
		]
		
		# Add a cross-attention block at the bottleneck
		if self.use_cross_attention:
			middle_layers.append(CrossModalAttentionBlock(ch, param_hidden_dim, num_heads=num_heads))
			
		middle_layers.append(ResidualBlock(ch, ch, time_embed_dim, dropout))
		
		self.middle_block = TimestepEmbedSequential(*middle_layers)
	
		# Property predictor MLP
		self.property_predictor = nn.Sequential(
			nn.AdaptiveAvgPool2d(1),  # Global average pooling
			nn.Flatten(),
			nn.Linear(ch, prop_hidden_dim),
			nn.SiLU(),
			nn.Linear(prop_hidden_dim, prop_dim)
		)
	
	def forward(self, x, params, prop, timesteps):
		"""
		Apply the model to an input batch.
		:param x: an [N x C x H x W] Tensor of image inputs.
		:param params: an [N x param_dim] Tensor of parameter inputs.
		:param timesteps: a 1-D batch of timesteps.
		:return: tuple of:
				- image noise prediction [N x C x H x W]
				- parameter noise prediction [N x param_dim]
		"""
		hs = []
		# Time step embedding
		emb = self.time_embed(timestep_embedding(timesteps, self.model_channels))
		
		# Process parameters
		param_features = self.param_encoder(params, emb)
		
		# Down stage for image
		h = x
		for module in self.down_blocks:
			h = module(h, emb, param_features)
			hs.append(h)
			
		# Middle stage
		h = self.middle_block(h, emb, param_features)
		
		return self.property_predictor(h)