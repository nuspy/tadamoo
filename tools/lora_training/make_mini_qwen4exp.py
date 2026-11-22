"""Writes mini_qwen4exp.gguf: a 4-layer random qwen4exp model (no PLE, no MTP) to test LoRA training in QVAC Fabric. Run from the fork's root."""
import sys
sys.path.insert(0, "gguf-py")
import numpy as np
import gguf

ARCH = "qwen4exp"
N_EMBD = 32
N_LAYER = 4
N_VOCAB = 259  # 3 control tokens (bos/eos/unk) + 256 byte-fallback tokens
N_EXPERT = 4
N_EXPERT_USED = 2
N_FF_EXP = 16
N_FF_SHEXP = 16
HC = 2
HC_LR = 4
HC_DIM = HC * N_EMBD
SSM_D_CONV = 4
SSM_D_STATE = 8      # head_k_dim == head_v_dim
SSM_DT_RANK = 4      # n_v_heads
SSM_N_GROUP = 2       # n_k_heads
KEY_DIM = SSM_D_STATE * SSM_N_GROUP     # 16
VALUE_DIM = SSM_D_STATE * SSM_DT_RANK   # 32
CONV_DIM = KEY_DIM * 2 + VALUE_DIM      # 64
N_HEAD = 2
N_HEAD_KV = 1
HEAD_K_DIM = 8
HEAD_V_DIM = 8
INDEXER_N_HEAD = 1
INDEXER_HEAD_DIM = 8
INDEXER_TOP_K = 4
INDEXER_COMPRESS_RATIO = 2
RECURRENT = [True, True, True, False]  # layer 3 = full attention

rng = np.random.default_rng(0)

def rnd(*shape):
    return rng.standard_normal(shape, dtype=np.float32) * 0.02

w = gguf.GGUFWriter("mini_qwen4exp.gguf", ARCH)

# --- generic hparams ---
w.add_context_length(512)
w.add_embedding_length(N_EMBD)
w.add_block_count(N_LAYER)
w.add_vocab_size(N_VOCAB)
w.add_expert_count(N_EXPERT)
w.add_expert_used_count(N_EXPERT_USED)
w.add_head_count(N_HEAD)
w.add_head_count_kv(N_HEAD_KV)
w.add_key_length(HEAD_K_DIM)
w.add_value_length(HEAD_V_DIM)
w.add_layer_norm_rms_eps(1e-6)
w.add_rope_dimension_sections([2, 2, 2, 2])
w.add_recurrent_layers(RECURRENT)

# --- expert feed-forward sizes ---
w.add_expert_feed_forward_length(N_FF_EXP)
w.add_expert_shared_feed_forward_length(N_FF_SHEXP)

# --- SSM / GDN ---
w.add_ssm_conv_kernel(SSM_D_CONV)
w.add_ssm_inner_size(N_EMBD)
w.add_ssm_state_size(SSM_D_STATE)
w.add_ssm_time_step_rank(SSM_DT_RANK)
w.add_ssm_group_count(SSM_N_GROUP)

# --- hyper-connection ---
w.add_hyper_connection_count(HC)
w.add_hyper_connection_low_rank(HC_LR)

# --- QSA indexer ---
w.add_indexer_head_count(INDEXER_N_HEAD)
w.add_indexer_key_length(INDEXER_HEAD_DIM)
w.add_indexer_top_k(INDEXER_TOP_K)
w.add_attention_compress_ratios([0, 0, 0, INDEXER_COMPRESS_RATIO])

# --- tokenizer (minimal SPM-style): byte-level vocab (256 bytes + bos/eos/unk), so any
# text falls back to single-byte tokens instead of the SPM trie hanging on no match
tokens = ["<bos>", "<eos>", "<unk>"] + [f"<0x{i:02X}>" for i in range(256)]
assert len(tokens) == N_VOCAB
scores = [0.0] * N_VOCAB
types = [gguf.TokenType.CONTROL, gguf.TokenType.CONTROL, gguf.TokenType.UNKNOWN] + [gguf.TokenType.BYTE] * 256
w.add_tokenizer_model("llama")
w.add_token_list(tokens)
w.add_token_scores(scores)
w.add_token_types(types)
w.add_bos_token_id(0)
w.add_eos_token_id(1)
w.add_unk_token_id(2)

# --- tensors ---
def add(name, np_shape):
    if "_norm" in name:
        # norm gains: 1 (the converter stores the folded 1 + w)
        t = np.ones(np_shape, dtype=np.float32)
    elif len(np_shape) >= 2:
        # projections: unit-variance-preserving scale, so the signal reaches the head
        t = (rng.standard_normal(np_shape, dtype=np.float32) / np.sqrt(np_shape[-1])).astype(np.float32)
    else:
        t = rnd(*np_shape)
    w.add_tensor(name, t)

add("token_embd.weight", (N_VOCAB, N_EMBD))
add("output.weight", (N_VOCAB, N_EMBD))
add("output_hc_norm.weight", (HC, N_EMBD))
add("output_hc_down.weight", (HC_LR, HC_DIM))
add("output_hc_up.weight", (HC_DIM, HC_LR))

for il in range(N_LAYER):
    p = f"blk.{il}"
    add(f"{p}.hc_attn_norm.weight", (HC, N_EMBD))
    add(f"{p}.hc_attn_down.weight", (HC_LR, HC_DIM))
    add(f"{p}.hc_attn_up.weight", (HC_DIM, HC_LR))
    add(f"{p}.hc_attn_inject.weight", (HC, HC_DIM))
    add(f"{p}.hc_ffn_norm.weight", (HC, N_EMBD))
    add(f"{p}.hc_ffn_down.weight", (HC_LR, HC_DIM))
    add(f"{p}.hc_ffn_up.weight", (HC_DIM, HC_LR))
    add(f"{p}.hc_ffn_inject.weight", (HC, HC_DIM))

    if not RECURRENT[il]:
        # full attention (QSA) layer
        add(f"{p}.attn_q.weight", (HEAD_K_DIM * N_HEAD * 2, N_EMBD))
        add(f"{p}.attn_k.weight", (HEAD_K_DIM * N_HEAD_KV, N_EMBD))
        add(f"{p}.attn_v.weight", (HEAD_V_DIM * N_HEAD_KV, N_EMBD))
        add(f"{p}.attn_output.weight", (N_EMBD, HEAD_K_DIM * N_HEAD))
        add(f"{p}.attn_q_norm.weight", (HEAD_K_DIM,))
        add(f"{p}.attn_k_norm.weight", (HEAD_K_DIM,))
        add(f"{p}.indexer.q_proj.weight", (INDEXER_N_HEAD * INDEXER_HEAD_DIM, N_EMBD))
        add(f"{p}.indexer.k_proj.weight", (INDEXER_HEAD_DIM, N_EMBD))
        add(f"{p}.indexer.q_norm.weight", (INDEXER_HEAD_DIM,))
        add(f"{p}.indexer.k_norm.weight", (INDEXER_HEAD_DIM,))
    else:
        add(f"{p}.attn_qkv.weight", (KEY_DIM * 2 + VALUE_DIM, N_EMBD))
        add(f"{p}.attn_gate.weight", (VALUE_DIM, N_EMBD))
        add(f"{p}.ssm_conv1d.weight", (CONV_DIM, SSM_D_CONV))
        add(f"{p}.ssm_dt.bias", (SSM_DT_RANK,))
        add(f"{p}.ssm_a", (SSM_DT_RANK,))
        add(f"{p}.ssm_beta.weight", (SSM_DT_RANK, N_EMBD))
        add(f"{p}.ssm_alpha.weight", (SSM_DT_RANK, N_EMBD))
        add(f"{p}.ssm_norm.weight", (HEAD_V_DIM,))
        add(f"{p}.ssm_out.weight", (N_EMBD, VALUE_DIM))

    add(f"{p}.ffn_gate_inp.weight", (N_EXPERT, N_EMBD))
    add(f"{p}.ffn_down_exps.weight", (N_EXPERT, N_EMBD, N_FF_EXP))
    add(f"{p}.ffn_gate_exps.weight", (N_EXPERT, N_FF_EXP, N_EMBD))
    add(f"{p}.ffn_up_exps.weight", (N_EXPERT, N_FF_EXP, N_EMBD))
    add(f"{p}.ffn_gate_inp_shexp.weight", (N_EMBD,))
    add(f"{p}.ffn_gate_shexp.weight", (N_FF_SHEXP, N_EMBD))
    add(f"{p}.ffn_up_shexp.weight", (N_FF_SHEXP, N_EMBD))
    add(f"{p}.ffn_down_shexp.weight", (N_EMBD, N_FF_SHEXP))

w.write_header_to_file()
w.write_kv_data_to_file()
w.write_tensors_to_file()
w.close()
print("wrote mini_qwen4exp.gguf")
