import math
import os
import tempfile
import time
from pathlib import Path

import numpy as np
import torch.nn
import torch.optim
import tqdm
import yaml
from omegaconf import DictConfig
from tensordict.nn import TensorDictSequential
from torchrl._utils import logger as torchrl_logger
from torchrl.collectors import MultiaSyncDataCollector
from torchrl.data import TensorDictPrioritizedReplayBuffer, LazyMemmapStorage
from torchrl.envs import MultiStepTransform
from torchrl.modules import EGreedyModule
from torchrl.objectives import HardUpdate, DQNLoss
from torchrl.record.loggers import get_logger

from rl.dqn.dqn_utils import make_dqn_model
from torchrl_utils import (
    CustomDQNLoss,
    value_rescale_inv,
    make_env
)

base_dir = Path(__file__).parent.parent.parent
algo_name = 'dqn'


def main(cfg: "DictConfig"):  # noqa: F821
    ckpt_dir = time.strftime('%Y%m%d_%H%M%S', time.localtime())[2:]
    if cfg.ckpt_name:
        ckpt_dir += f'_{cfg.ckpt_name}'
    if not Path(f'{base_dir}/ckpt').exists():
        os.mkdir(f'{base_dir}/ckpt')
    if not Path(f'{base_dir}/ckpt/{algo_name}').exists():
        os.mkdir(f'{base_dir}/ckpt/{algo_name}')
    if not Path(f'{base_dir}/ckpt/{algo_name}/{ckpt_dir}').exists():
        os.mkdir(f'{base_dir}/ckpt/{algo_name}/{ckpt_dir}')
    device = cfg.device
    if device in ("", None):
        if torch.cuda.is_available():
            device = "cuda:0"
        else:
            device = "cpu"
    device = torch.device(device)

    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)

    # Make the components
    if cfg.pretrained_model:
        model = torch.load(f'{base_dir}/{cfg.pretrained_model}').to(device)
    else:
        model = make_dqn_model()

    greedy_module = EGreedyModule(
        annealing_num_steps=cfg.collector.annealing_frames,
        eps_init=cfg.collector.eps_start,
        eps_end=cfg.collector.eps_end,
        spec=model.spec,
    )
    model_explore = TensorDictSequential(
        model,
        greedy_module,
    ).to(device)

    # Create the collector
    collector = MultiaSyncDataCollector(
        create_env_fn=[lambda: make_env(
            num_envs=1,
            device='cpu',
        )] * cfg.collector.num_envs,
        policy=model_explore,
        frames_per_batch=cfg.collector.frames_per_batch,
        total_frames=cfg.collector.total_frames,
        device='cpu',
        storing_device='cpu',
        max_frames_per_traj=-1,
        init_random_frames=cfg.collector.init_random_frames,
    )

    # Create the replay buffer
    tempdir = tempfile.TemporaryDirectory()
    scratch_dir = tempdir.name
    replay_buffer = TensorDictPrioritizedReplayBuffer(
        alpha=0.7,
        beta=0.5,
        pin_memory=False,
        prefetch=10,
        storage=LazyMemmapStorage(
            max_size=cfg.buffer.buffer_size,
            scratch_dir=scratch_dir,
        ),
        batch_size=cfg.buffer.batch_size,
        transform=MultiStepTransform(n_steps=cfg.loss.nstep, gamma=cfg.loss.gamma) if cfg.loss.nstep > 1 else None,
    )

    # Create the loss module
    if cfg.loss.use_value_rescale:
        loss_module = CustomDQNLoss(
            value_network=model,
            loss_function=cfg.loss.loss_type,
            delay_value=True,
            double_dqn=True,
        )
    else:
        loss_module = DQNLoss(
            value_network=model,
            loss_function=cfg.loss.loss_type,
            delay_value=True,
            double_dqn=True,
        )
    loss_module.make_value_estimator(
        gamma=cfg.loss.gamma,
    )
    loss_module = loss_module.to(device)
    target_net_updater = HardUpdate(
        loss_module, value_network_update_interval=cfg.loss.hard_update_freq
    )

    # Create the optimizer
    optimizer = torch.optim.Adam(loss_module.parameters(), lr=cfg.optim.lr)

    # Create the logger
    logger = None
    if cfg.logger.backend:
        if cfg.logger.backend == 'wandb':
            logger = get_logger(
                cfg.logger.backend,
                logger_name=f'{base_dir}/ckpt',
                experiment_name=ckpt_dir,
                wandb_kwargs={
                    "config": dict(cfg),
                },
            )
        else:
            logger = get_logger(
                cfg.logger.backend,
                logger_name=f'{base_dir}/ckpt/{algo_name}',
                experiment_name=ckpt_dir,
            )

    # Main loop
    collected_frames = 0
    start_time = time.time()
    batch_size = cfg.buffer.batch_size
    test_interval = cfg.logger.test_interval
    frames_per_batch = cfg.collector.frames_per_batch
    pbar = tqdm.tqdm(total=cfg.collector.total_frames)
    init_random_frames = cfg.collector.init_random_frames
    num_updates = math.ceil(frames_per_batch / batch_size * cfg.loss.utd_ratio)
    sampling_start = time.time()
    q_losses = torch.zeros(num_updates, device=device)

    for i, data in enumerate(collector):
        log_info = {}
        sampling_time = time.time() - sampling_start
        pbar.update(data.numel())
        data = data.reshape(-1)
        current_frames = data.numel()
        collected_frames += current_frames
        greedy_module.step(current_frames)

        # Get and log training rewards and episode lengths
        episode_rewards = data["next", "episode_reward"][data["next", "done"]]
        if len(episode_rewards) > 0:
            episode_reward_mean = episode_rewards.mean().item()
            episode_length = data["next", "step_count"][data["next", "done"]]
            episode_length_mean = episode_length.sum().item() / len(episode_length)
            episode_weed_ratio = data["next", "weed_ratio"][data["next", "done"]]
            episode_weed_ratio_mean = episode_weed_ratio.sum().item() / len(episode_length)
            log_info.update(
                {
                    "train/episode_reward": episode_reward_mean,
                    "train/episode_length": episode_length_mean,
                    "train/episode_weed_ratio": episode_weed_ratio_mean,
                }
            )
        data.pop('weed_ratio')
        data.pop(('next', 'weed_ratio'))
        replay_buffer.extend(data)

        if collected_frames < init_random_frames:
            if logger:
                for key, value in log_info.items():
                    logger.log_scalar(key, value, step=collected_frames)
            continue

        # optimization steps
        training_start = time.time()
        for j in range(num_updates):
            sampled_tensordict = replay_buffer.sample(batch_size)
            sampled_tensordict = sampled_tensordict.to(device)
            loss_td = loss_module(sampled_tensordict)
            q_loss = loss_td["loss"]
            optimizer.zero_grad()
            q_loss.backward()
            if cfg.optim.max_grad_norm:
                torch.nn.utils.clip_grad_norm_(
                    list(loss_module.parameters()), max_norm=cfg.optim.max_grad_norm
                )
            optimizer.step()
            target_net_updater.step()
            q_losses[j].copy_(q_loss.detach())
            replay_buffer.update_tensordict_priority(sampled_tensordict)
        training_time = time.time() - training_start

        # Get and log q-values, loss, epsilon, sampling time and training time

        log_info.update(
            {
                "train/q_loss": q_losses.mean().item(),
                "train/epsilon": greedy_module.eps,
                "train/sampling_time": sampling_time,
                "train/training_time": training_time,
            }
        )
        if cfg.loss.use_value_rescale:
            log_info.update(
                {
                    "train/q_values": value_rescale_inv((data["action_value"] * data["action"]).sum()).item()
                                      / frames_per_batch,
                    "train/q_logits": (data["action_value"] * data["action"]).sum().item()
                                      / frames_per_batch,
                }
            )
        else:
            log_info.update(
                {
                    "train/q_values": (data["action_value"] * data["action"]).sum().item()
                                      / frames_per_batch,
                }
            )
        # Get and log evaluation rewards and eval time
        prev_test_frame = ((i - 1) * frames_per_batch) // test_interval
        cur_test_frame = (i * frames_per_batch) // test_interval
        final = collected_frames >= collector.total_frames
        if (i > 0 and (prev_test_frame < cur_test_frame)) or final:
            model_name = str(collected_frames // 1000).rjust(5, '0')
            torch.save(
                model,
                f'{base_dir}/ckpt/{algo_name}/{ckpt_dir}/t[{model_name}].pt'
            )

        # Log all the information
        if logger:
            for key, value in log_info.items():
                logger.log_scalar(key, value, step=collected_frames)

        # update weights of the inference policy
        collector.update_policy_weights_()
        sampling_start = time.time()

    collector.shutdown()
    end_time = time.time()
    execution_time = end_time - start_time
    torchrl_logger.info(f"Training took {execution_time:.2f} seconds to finish")
    time.sleep(5)


if __name__ == "__main__":
    cfg = yaml.load(open(f'{base_dir}/configs/train_{algo_name}_config.yaml'), Loader=yaml.FullLoader)
    cfg = DictConfig(cfg)
    main(cfg)
