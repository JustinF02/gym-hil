#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any, Dict, Literal, Tuple
import sys
import mujoco
import numpy as np
from gymnasium import spaces

from gym_hil.mujoco_gym_env import FrankaGymEnv, GymRenderingSpec

_PANDA_HOME = np.asarray((0, 0.195, 0, -2.43, 0, 2.62, 0.785))
_CARTESIAN_BOUNDS = np.asarray([[0.2, -0.3, 0], [0.6, 0.3, 0.5]])
_SAMPLING_BOUNDS = np.asarray([[0.3, -0.15], [0.5, 0.15]])


class PandaStackCubesGymEnv(FrankaGymEnv):
    """Environment for a Panda robot picking up a cube."""

    def __init__(
        self,
        seed: int = 0,
        control_dt: float = 0.1,
        physics_dt: float = 0.002,
        render_spec: GymRenderingSpec = GymRenderingSpec(),  # noqa: B008
        render_mode: Literal["rgb_array", "human"] = "rgb_array",
        image_obs: bool = False,
        reward_type: str = "sparse",
        random_block_position: bool = True,
        num_blocks: int = 3, #JFD4 added num_blocks parameters
    ):
        self.reward_type = reward_type
        self.num_blocks = num_blocks

        super().__init__(
            seed=seed,
            control_dt=control_dt,
            physics_dt=physics_dt,
            render_spec=render_spec,
            render_mode=render_mode,
            image_obs=image_obs,
            home_position=_PANDA_HOME,
            cartesian_bounds=_CARTESIAN_BOUNDS,
        )

        # Task-specific setup
        self._block_z = self._model.geom("block1").size[2]
        self._random_block_position = random_block_position

        # Setup observation space properly to match what _compute_observation returns
        # Observation space design:
        #   - "state":  agent (robot) configuration as a single Box
        #   - "environment_state": block position in the world as a single Box
        #   - "pixels": (optional) dict of camera views if image observations are enabled

        agent_dim = self.get_robot_state().shape[0]
        agent_box = spaces.Box(-np.inf, np.inf, (agent_dim,), dtype=np.float32)
        env_box = spaces.Box(-np.inf, np.inf, (3,), dtype=np.float32)

        if self.image_obs:
            self.observation_space = spaces.Dict(
                {
                    "pixels": spaces.Dict(
                        {
                            "front": spaces.Box(
                                0,
                                255,
                                (self._render_specs.height, self._render_specs.width, 3),
                                dtype=np.uint8,
                            ),
                            "wrist": spaces.Box(
                                0,
                                255,
                                (self._render_specs.height, self._render_specs.width, 3),
                                dtype=np.uint8,
                            ),
                        }
                    ),
                    "agent_pos": agent_box,
                }
            )
        else:
            self.observation_space = spaces.Dict(
                {
                    "agent_pos": agent_box,
                    "environment_state": env_box,
                }
            )

    def reset(self, seed=None, **kwargs) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
        """Reset the environment."""
        # Ensure gymnasium internal RNG is initialized when a seed is provided
        super().reset(seed=seed)
        
        mujoco.mj_resetData(self._model, self._data)

        # Reset the robot to home position
        self.reset_robot()

        # # Sample a new block position
        # if self._random_block_position:
        #     block_xy = np.random.uniform(*_SAMPLING_BOUNDS)
        #     self._data.jnt("block").qpos[:3] = (*block_xy, self._block_z)
        # else:
        #     block_xy = np.asarray([0.5, 0.0])
        #     self._data.jnt("block").qpos[:3] = (*block_xy, self._block_z)
        # mujoco.mj_forward(self._model, self._data)

        for i in range(self.num_blocks):
            name = f"block{i}" if i > 0 else "block1"
            if self._random_block_position:
                block_xy = np.random.uniform(*_SAMPLING_BOUNDS)
            else:
                block_xy = np.asarray([0.5, -0.1 + i * 0.1])

            self._data.joint(name).qpos[:3] = (*block_xy, self._block_z)

        
        self._z_inits = []
        for i in range(self.num_blocks):
            name = f"block{i}" if i > 0 else "block1"
            z = self._data.sensor(f"{name}_pos").data[2]
            self._z_inits.append(z)
        self._prev_gripper_closed = False
        obs = self._compute_observation()
        return obs, {}

    def step(self, action: np.ndarray) -> Tuple[Dict[str, np.ndarray], float, bool, bool, Dict[str, Any]]:
        """Take a step in the environment."""
        # Apply the action to the robot
        self.apply_action(action)

        # Compute observation, reward and termination
        obs = self._compute_observation()
        rew = self._compute_reward()
        success = self._is_success()

        if self.reward_type == "sparse":
            success = rew == 1.0

        terminated = bool(success)
        # Check if block1 is outside bounds
        block1_pos = self._data.sensor("block1_pos").data
        exceeded_bounds = np.any(block1_pos[:2] < (_SAMPLING_BOUNDS[0] - 0.05)) or np.any(
            block1_pos[:2] > (_SAMPLING_BOUNDS[1] + 0.05)
        )
        
        terminated = terminated or exceeded_bounds

        return obs, rew, terminated, False, {"succeed": success}

    def _compute_observation(self) -> dict:
        """Compute the current observation."""
        # Create the dictionary structure that matches our observation space
        observation = {}

        # Get robot state
        robot_state = self.get_robot_state().astype(np.float32)

        # Assemble observation respecting the newly defined observation_space
        block1_pos = self._data.sensor("block1_pos").data.astype(np.float32)

        if self.image_obs:
            # Image observations
            front_view, wrist_view = self.render()
            observation = {
                "pixels": {"front": front_view, "wrist": wrist_view},
                "agent_pos": robot_state,
            }
        else:
            # State-only observations
            observation = {
                "agent_pos": robot_state,
                "environment_state": block1_pos,
            }

        return observation

    def _is_gripper_closed(self) -> bool:
        """Check if gripper is closed (holding something)."""
        left_finger = self._data.joint("left_driver_joint").qpos[0]
        #right_finger = self._data.joint("right_driver_joint").qpos[0]
        
        # The gripper is synchronized (same value for both), so we can use just one
        # Gripper is closed when the joint position is higher (fingers closer)
        gripper_position = left_finger  # or right_finger, they should be the same

        return gripper_position > 0.1

    def _was_block_just_released(self) -> bool:
        """Check if a block was just released from gripper."""
        # Store previous gripper state
        if not hasattr(self, '_prev_gripper_closed'):
            self._prev_gripper_closed = self._is_gripper_closed()
            return False
        
        current_closed = self._is_gripper_closed()
        just_released = self._prev_gripper_closed and not current_closed
        self._prev_gripper_closed = current_closed
        
        return just_released

    #one main issue : if the cube rotates, the computed value is offset.
    def _compute_reward(self) -> float:
        """Compute reward based on stacking progress."""
        if self.reward_type == "dense":
            
            # Get all block positions
            block_positions = [
                self._data.sensor(f"block{i + 1}_pos").data
                for i in range(self.num_blocks)
            ]
            
            # Reward for lifting any block, the max reward is kept (should be the block currently lifted)
            max_lift_reward = 0.0
            for i, pos in enumerate(block_positions):
                lift = (pos[2] - self._z_inits[i]) / (self._block_z * 2)
                lift_reward = np.clip(lift, 0.0, 1.0)
                max_lift_reward = max(max_lift_reward, lift_reward)
            
            # Reward for stacking, bigger if block has been released after stacked
            stack_reward = 0.0
            if self._was_block_just_released() and self._is_success():
                stack_reward = 2.0  # Big reward for successful release
            elif self._is_success():
                stack_reward = 0.5  # Smaller reward if stacked but still holding
            
            # 30% for lifting + 70% for stacking.
            return 0.3 * max_lift_reward + 0.7 * stack_reward
        else:
            # Sparse reward: only when released AND stacked
            if self._was_block_just_released() and self._is_success():
                return 1.0
            return 0.0

    def _is_success(self) -> bool:
        """Check if task is successfully completed (strict criteria)."""
        positions = [
            self._data.sensor(f"block{i + 1}_pos").data.copy()
            for i in range(self.num_blocks)
        ]
        
        stacked = False
        log_lines = []

        for i in range(self.num_blocks):
            name = f"block{i + 1}"
            qpos = self._data.joint(name).qpos[:3]
            log_lines.append(f"  - {name}: {np.array2string(qpos, precision=4)}")
        log_lines.append("")

        for i in range(self.num_blocks):
            for j in range(self.num_blocks):
                if i == j:
                    continue
                
                upper = positions[i]
                lower = positions[j]
                
                xy_dist = np.linalg.norm(upper[:2] - lower[:2])
                z_diff = upper[2] - lower[2]
                target_z = self._block_z * 2

                log_lines.append(
                    f"Block {i + 1} over {j+1} | xy={xy_dist:.4f}, z={z_diff:.4f} (target={target_z:.4f})"
                )

                
                if (xy_dist < 0.030 and # cube is 0.25 wide, detect if it is above another
                    z_diff > 0.03 and   # verify that it is above the other in z value
                    abs(z_diff - self._block_z * 2) < 0.05):
                    stacked = True
                    log_lines.append(f"block{i+1} is stacked on block{j+1}")
                    break
            if stacked:
                break
        
        sys.stdout.write("\x1b[2J\x1b[H")
        for line in log_lines:
            print(line)

        return stacked


if __name__ == "__main__":
    from gym_hil import PassiveViewerWrapper

    env = PandaStackCubesGymEnv(render_mode="human", num_blocks=3)
    env = PassiveViewerWrapper(env)
    env.reset()
    for _ in range(100):
        env.step(np.random.uniform(-1, 1, 7))
    env.close()
