import multiprocessing
from x_transformers import XTransformer
import numpy as np
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from  subprocess import PIPE, Popen, run
import os
import sentencepiece as spm
import random
import sys
import string
import platform
import ast
import csv
import gzip
import base64
import shutil
import time
from functools import wraps
import torch
import tqdm

ARCH = 'x86'

if platform.system() == 'Linux':
  if ARCH == 'riscv':
    CC = 'riscv64-linux-gnu-gcc'
    STRIP = 'riscv64-linux-gnu-strip'
    OBJDUMP = 'riscv64-linux-gnu-objdump'
  elif ARCH == 'x86':
    CC = 'gcc'
    STRIP = 'strip'
    OBJDUMP = 'objdump'
elif platform.system() == 'Darwin':
  if ARCH == 'riscv':
    CC = 'riscv64-elf-gcc'
    STRIP = 'riscv64-elf-strip'
    OBJDUMP = 'riscv64-elf-objdump'
  elif ARCH == 'x86':
    CC = 'x86_64-elf-gcc'
    STRIP = 'x86_64-elf-strip'
    OBJDUMP = 'x86_64-elf-objdump'
  elif ARCH == 'aarch64':
    CC = 'aarch64-elf-gcc'
    STRIP = 'aarch64-elf-strip'
    OBJDUMP = 'aarch64-elf-objdump'




GENERATE_EVERY = 1000
LEARNING_RATE = 1e-4
NUM_BATCHES = int(1e5)
NUM_TOKENS = 8194
ENC_SEQ_LEN = 1024
DEC_SEQ_LEN = 1024
ROOTDIR = os.path.abspath(os.path.join(os.path.dirname(__file__),'..','..'))
TMP = '/tmp/sopt'
DICTIONARY = f'{ROOTDIR}/misc/zstd_x86_dictionary'


def timeit(func):
  @wraps(func)
  def timeit_wrapper(*args, **kwargs):
    start_time = time.perf_counter()
    result = func(*args, **kwargs)
    end_time = time.perf_counter()
    total_time = end_time - start_time
    print(f'Function {func.__name__}{args} {kwargs} Took {total_time:.4f} seconds')
    return result
  return timeit_wrapper

nvmlInit_called = False

def report_cuda_size():
  global nvmlInit_called
  if torch.cuda.is_available():
    from pynvml import nvmlInit, nvmlDeviceGetHandleByIndex, nvmlDeviceGetMemoryInfo
    if not nvmlInit_called:
      nvmlInit()
      nvmlInit_called = True
    h = nvmlDeviceGetHandleByIndex(0)
    info = nvmlDeviceGetMemoryInfo(h)
    print(f'cuda total    : {info.total // 1024 // 1024}MB')
    print(f'cuda free     : {info.free // 1024 // 1024}MB')
    print(f'cuda used     : {info.used // 1024 // 1024}MB')

def randstring(n):
  return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def tkn_sp(t):
  if t == 'DECSTART':
    return 8192
  elif t == 'PAD':
    return 8193
  assert False

def sentencepiece_train():
  with open(f'{TMP}/sentencepiece.txt', 'w+t') as outf:
    for db_idx in range(10):
      with gzip.open(f'/{ROOTDIR}/cleandata/processed_{db_idx}.csv.gz', 'rt') as f:
        reader = csv.DictReader(f)
        for entry in reader:
          unopt = ast.literal_eval(entry['unopt'])
          opt = ast.literal_eval(entry['opt'])
          outf.write(base64.b64encode(unopt).decode('utf-8') + '\n')
          outf.write(base64.b64encode(opt).decode('utf-8') + '\n')

sp = None

def tokenize_sp(data: bytes):
  global sp
  if sp is None:
    sp = spm.SentencePieceProcessor()
    sp.load(f'{ROOTDIR}/misc/x86_zstd_sopt.model')
  tokens = sp.encode(base64.b64encode(data).decode('utf-8'))
  return tokens


def detokenize_sp(tokens: [int]):
  global sp
  if sp is None:
    sp = spm.SentencePieceProcessor()
    sp.load(f'{ROOTDIR}/misc/x86_zstd_sopt.model')
  tokens = [t for t in tokens if t < NUM_TOKENS-2]
  tokens = sp.decode(tokens)
  try:
    tokens = base64.b64decode(tokens)
  except:
    tokens = "invalid".encode('utf-8')
  return tokens

def zstd_compress(data: bytes, dictionary: str) -> bytes:
  ret = run(f"zstd -D {dictionary} --ultra -22 -c -".split(), input=data,  stdout=PIPE, stderr=PIPE)
  return ret.stdout

def zstd_decompress(data: [int], dictionary: str) -> bytes:
  data = [x for x in data if 0 <= x <= 255]
  ret = run(f"zstd -D {dictionary} --ultra -22 -d -c -".split(), input=bytes(data),  stdout=PIPE, stderr=PIPE)
  if len(ret.stderr) > 0:
    return ret.stderr + bytes(data)
  return ret.stdout


def cycle(device, training_data, db_idx, batch_size):
  if len(training_data) < batch_size:
    print("db_idx", db_idx)
    with gzip.open(f'/{ROOTDIR}/cleandata/processed_{db_idx}.csv.gz','rt') as f:
      reader = csv.DictReader(f)
      for entry in reader:
        unopt = tokenize_sp(ast.literal_eval(entry['unopt']))
        opt = tokenize_sp(ast.literal_eval(entry['opt']))
        if len(unopt) >= ENC_SEQ_LEN or len(opt) >= DEC_SEQ_LEN:
          continue
        opt.insert(0,tkn_sp('DECSTART'))
        mask = [True]*len(unopt)
        mask.extend([False]*(ENC_SEQ_LEN-len(unopt)))
        unopt.extend([tkn_sp('PAD')] * (ENC_SEQ_LEN - len(unopt)))
        opt.extend([tkn_sp('PAD')] * (DEC_SEQ_LEN - len(opt)))
        training_data.append([unopt, opt, mask])
      db_idx += 1
      if not os.path.exists(f'/{ROOTDIR}/data/processed_{db_idx}.csv.gz'):
        db_idx = 0
  batch = training_data[:batch_size]
  training_data = training_data[batch_size:]
  mysrc = torch.tensor(list(x[0] for x in batch)).long().to(device)
  mytgt = torch.tensor(list(x[1] for x in batch)).long().to(device)
  mysrc_mask = torch.tensor(list(x[2] for x in batch)).bool().to(device)
  return mysrc, mysrc_mask, mytgt, training_data, db_idx


def gen(uuid):
  ALL_INPUTS = set()
  outpath = f'/{ROOTDIR}/rawdata/db_{randstring(16)}.csv.gz'
  with gzip.open(outpath,'w+t') as f:
    writer = csv.DictWriter(f,['c','unopt','opt'])
    writer.writeheader()
    for x in range(1000):
      if uuid == 0 and x % 10 == 0:
          print(x)

      yarpgen = run(f'/{ROOTDIR}/bin/{platform.system()}/yarpgen --std=c -o /{TMP}/yarpgen_{uuid}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      unopt = run(f'{CC} -o /{TMP}/yarpgen_{uuid}/func.c.unopt.o -O0 -Wall -fcf-protection=none -march=znver3 -xc -c /{TMP}/yarpgen_{uuid}/func.c'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      opt = run(f'{CC} -o /{TMP}/yarpgen_{uuid}/func.c.opt.o -O3 -Wall -fcf-protection=none -march=znver3 -xc -c /{TMP}/yarpgen_{uuid}/func.c'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      unopt = run(f'{STRIP} /{TMP}/yarpgen_{uuid}/func.c.unopt.o'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      opt = run(f'{STRIP} /{TMP}/yarpgen_{uuid}/func.c.opt.o'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)

      with  open(f'/{TMP}/yarpgen_{uuid}/func.c') as f:
        prog = f.read()
      with open(f'/{TMP}/yarpgen_{uuid}/func.c.unopt.o', 'rb') as f:
        unopt = f.read()
      with open(f'/{TMP}/yarpgen_{uuid}/func.c.opt.o', 'rb') as f:
        opt = f.read()

      if h := hash(unopt) in ALL_INPUTS:
        continue
      ALL_INPUTS.add(h)
      if len(unopt) > 16384 or len(opt) > 16384:
        print("skipping too long prog")
        continue
      writer.writerow({'c': prog, 'unopt': unopt, 'opt': opt})
  return outpath

def clean_database(files):
  print("cleaning database")
  ALL_INPUTS = set()
  i = len(os.listdir(f'/{ROOTDIR}/cleandata'))
  for gz in files:
    print(f"cleaning {gz}")
    out = list()
    with gzip.open(gz, 'rt') as inf:
      reader = csv.DictReader(inf)
      for row in reader:
        if h := hash(row['unopt']) not in ALL_INPUTS:
          ALL_INPUTS.add(h)
          out.append(row)
    with gzip.open(f'/{ROOTDIR}/cleandata/processed_{i}.csv.gz', 'w+t') as outf:
      writer = csv.DictWriter(outf, ['c', 'unopt', 'opt'])
      writer.writeheader()
      writer.writerows(out)
      i+=1


def generate_database():
  print("generating database")
  ncpu = multiprocessing.cpu_count()
  os.makedirs(f'{TMP}/data', exist_ok=True)
  os.makedirs(f'{TMP}/all_yarpgen', exist_ok=True)
  os.makedirs(f'{ROOTDIR}/rawdata', exist_ok=True)
  os.makedirs(f'{ROOTDIR}/cleandata', exist_ok=True)
  for uuid in range(ncpu):
    os.makedirs(f'{TMP}/yarpgen_{uuid}', exist_ok=True)
  print(f"spawning {ncpu} threads")
  for x in range(100):
    print('processed', x)
    with multiprocessing.Pool(ncpu) as p:
      ret = p.map(gen, list(range(ncpu)))
    #ret = gen(0)
    clean_database(ret)


def get_model(device, pad_value, num_tokens, rank, world_size):
  if device == 'cuda':
    if '2060' in torch.cuda.get_device_name():
      dim = 512
      batch_size = 8
      generate_every = 100
      enc_depth = 4
      enc_heads = 4
      dec_depth = 4
      dec_heads = 4
      dtype = torch.float16
    elif 'V100' in torch.cuda.get_device_name():
      dim = 1024
      batch_size = 32
      generate_every = 500
      enc_depth = 8
      enc_heads = 8
      dec_depth = 8
      dec_heads = 8
      dtype = torch.float16
    elif ('4090' in torch.cuda.get_device_name() or
          'A5000' in torch.cuda.get_device_name() or
          '3090' in torch.cuda.get_device_name()):
      dim = 2048
      batch_size = 32
      generate_every = 1000
      enc_depth = 10
      enc_heads = 10
      dec_depth = 10
      dec_heads = 10
      dtype = torch.bfloat16
    elif 'A6000' in torch.cuda.get_device_name():
      dim = 1536
      batch_size = 32
      generate_every = 1000
      enc_depth = 12
      enc_heads = 12
      dec_depth = 12
      dec_heads = 12
      dtype = torch.bfloat16
    elif 'A100' in torch.cuda.get_device_name():
      dim = 2048
      batch_size = 64
      generate_every = 2000
      enc_depth = 20
      enc_heads = 20
      dec_depth = 20
      dec_heads = 20
      dtype = torch.bfloat16
    else:
      assert False
  else:
    dim = 512
    batch_size = 4
    generate_every = 100
    enc_depth = 4
    enc_heads = 4
    dec_depth = 4
    dec_heads = 4
    dtype = torch.float16

  model = XTransformer(
    dim=dim,
    pad_value=pad_value,
    tie_token_emb=True,
    enc_attn_flash=True,
    dec_attn_flash=True,
    return_tgt_loss=True,
    enc_num_tokens=num_tokens,
    enc_depth=enc_depth,
    enc_heads=enc_heads,
    enc_max_seq_len=ENC_SEQ_LEN,
    dec_num_tokens=num_tokens,
    dec_depth=dec_depth,
    dec_heads=dec_heads,
    dec_max_seq_len=DEC_SEQ_LEN,
    attn_num_mem_kv=16,
    num_memory_tokens=20,
    use_simple_rmsnorm=True,
    ff_no_bias=True,
    ff_swish=True,
    ff_glu=True,
    attn_kv_heads=2,
    attn_gate_values=True,
    sandwich_coef=6,
    shift_tokens=1,
    use_abs_pos_emb=False,
    rotary_xpos=True,
    attn_sparse_topk=8,
    attn_talking_heads=True,
    attn_on_attn=True,
    macaron=True,
    gate_residual=True,
    dynamic_pos_bias=True,
    dynamic_pos_bias_log_distance=True,
    resi_dual=True,
    resi_dual_scale=0.1, )

  if device == 'cuda':
    model = model.cuda(device=rank)
  elif device == 'mps':
    model = model.to('mps')
  elif device == 'cpu':
    model = model.to('cpu')

  if world_size > 1:
    model = FSDP(model, use_orig_params=True)

  #if device in ['cuda']:
  #  model = torch.compile(model)

  model_parameters = filter(lambda p: p.requires_grad, model.parameters())
  params = sum([np.prod(p.size()) for p in model_parameters])
  print(f"num params {params // 1024 // 1024}M {params // 1024}K ")

  return model, dtype, batch_size, generate_every


@timeit
def train(rank, world_size, device):

  if world_size > 1:
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'
    torch.distributed.init_process_group(backend='nccl', rank=rank,world_size=world_size)
    torch.cuda.set_device(rank)

  model, dtype, batch_size, generate_every = get_model(device, tkn_sp('PAD'), NUM_TOKENS,rank,world_size)

  optim = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
  if device == 'cuda':
    scaler = torch.cuda.amp.GradScaler()
  scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optim,T_0=100)

  training_data = []
  db_idx = rank

  iterations = 0
  if device == 'cuda' and os.path.exists(f'/{ROOTDIR}/checkpoint-{torch.cuda.get_device_name()}.pt'):
      print(f"loading /{ROOTDIR}/checkpoint-{torch.cuda.get_device_name()}.pt")
      checkpoint = torch.load(f'/{ROOTDIR}/checkpoint-{torch.cuda.get_device_name()}.pt')
      model.load_state_dict(checkpoint['model_state_dict'])
      optim.load_state_dict(checkpoint['optimizer_state_dict'])
      iterations = checkpoint['iterations']
      loss = checkpoint['loss']
      db_idx = checkpoint['db_idx']

  for i in tqdm.tqdm(range(iterations,NUM_BATCHES), mininterval=10., desc='training'):
    model.train()
    optim.zero_grad()

    src, src_mask, tgt, training_data, db_idx = cycle(device, training_data, db_idx, batch_size)
    if device == 'cuda':
      with torch.cuda.amp.autocast(dtype=dtype):
        loss = model(src, tgt, mask=src_mask)
      scaler.scale(loss).backward()
      scaler.step(optim)
      scaler.update()
    elif device in ['mps','cpu']:
      loss = model(src, tgt, mask=src_mask)
      loss.backward()
      optim.step()
    scheduler.step(i/NUM_BATCHES)
    print(f'{i}: {loss.item()}')

    if i == 0 and device == 'cuda':
      report_cuda_size()
    if i % GENERATE_EVERY == 0:
      with FSDP.summon_full_params(model, writeback=False, recurse=False):
        if device == 'cuda':
          torch.save({
            'iterations':i,
            'model_state_dict':model.state_dict(),
            'optimizer_state_dict':optim.state_dict(),
            'loss':loss.item(),
            'scaler':scaler.state_dict(),
            'scheduler':scheduler.state_dict(),
            'db_idx':db_idx},
            f'/{ROOTDIR}/checkpoint-{torch.cuda.get_device_name()}.pt')
    if i % GENERATE_EVERY == 0:
      model.eval()
      src, src_mask, tgt, training_data, db_idx = cycle(device, training_data, db_idx, batch_size)
      src, src_mask, tgt  = src[:1], src_mask[:1], tgt[:1]
      start_tokens = torch.tensor([tkn_sp('DECSTART')]).to(device)
      sample = model.generate(src, start_tokens, DEC_SEQ_LEN, mask = src_mask)
      #the target output always includes the 'DECSTART' token whereas the sampled output does not
      #so shift the output left one token to delete it
      for x in range(DEC_SEQ_LEN-1):
        tgt[0,x] = tgt[0,x+1]
      incorrects = (tgt != sample).sum()
      print_stmt = f'\nRANK: {rank} start\n'
      print_stmt += f"\ninput tokenized:  \n{detokenize_sp(src.tolist()[0])} \n"
      print_stmt += f"\npredicted detokenized:  \n{detokenize_sp(sample.tolist())}\n"
      print_stmt += f"\nactual detokenized:     \n{detokenize_sp(tgt.tolist()[0])}\n"
      print_stmt += f"\nincorrects: {incorrects}\n"
      print_stmt += f'\nRANK: {rank} end\n'
      print(print_stmt)

  if world_size > 1:
    torch.distributed.destroy_process_group()

def main():
  if torch.backends.mps.is_available() and torch.backends.mps.is_built():
    #device = 'mps'
    device = 'cpu'
    world_size = 1
  elif torch.cuda.is_available():
    device = 'cuda'
    world_size = torch.cuda.device_count()
  else:
    device = 'cpu'
    world_size = 1
  if world_size <= 1:
    train(0,1,device)
  else:
    print(f"spawning {world_size} gpu threads")
    torch.multiprocessing.spawn(train, args=(world_size,device), nprocs=world_size,join=True)

if __name__ == '__main__':
  if len(sys.argv) != 2:
    print("you must specify a trask: train, infer, gen, clean, or sentencepiece")
    print("defaulting to train")
    sys.argv.append("sentencepiece")
  if sys.argv[1] == 'train':
    main()
  elif sys.argv[1] == 'gen':
    generate_database()
  elif sys.argv[1] == 'infer':
    pass
  elif sys.argv[1] == 'sentencepiece':
    sentencepiece_train()
