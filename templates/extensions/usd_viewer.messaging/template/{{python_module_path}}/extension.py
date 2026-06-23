# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

import asyncio

from .stage_loading import LoadingManager
from .stage_management import StageManager
from .custom_messaging import CustomMessageManager  # ADD THIS IMPORT
from .usd_spawner import _cfg as _usd_cfg
from .api_server import start_api_server, stop_api_server
from .cctv_capture import get_cctv_capture
from .robots.robot_controller import get_robot_controller
import omni.ext


# Any class derived from `omni.ext.IExt` in top level module (defined in
# `python.modules` of `extension.toml`) will be instantiated when extension
# gets enabled and `on_startup(ext_id)` will be called. Later when extension
# gets disabled on_shutdown() is called.
class Extension(omni.ext.IExt):
    """This extension manages creating the loading, stage, and custom
    messaging managers"""  # UPDATED DOCSTRING

    def on_startup(self):
        """This is called every time the extension is activated."""
        # Internal messaging state
        self._loading_manager: LoadingManager = LoadingManager()
        self._stage_manager: StageManager = StageManager()
        self._custom_manager: CustomMessageManager = CustomMessageManager(
            agent_backend_url=_usd_cfg.get("backend_url", "http://localhost:8000"),
        )
        # Expose manager instance for the API server to access
        from . import custom_messaging as _cm_mod
        _cm_mod._manager_instance = self._custom_manager

        # Start API HTTP server (allows agent backend to capture frames directly)
        asyncio.ensure_future(start_api_server(port=8100))

        # Initialize robot controller (starts update loop)
        self._robot_controller = get_robot_controller()
        self._robot_controller.initialize()
        self._robot_controller.start_camera_stream(backend_url=_usd_cfg.get("backend_url", "http://localhost:8000"))

    def on_shutdown(self):
        """This is called every time the extension is deactivated. It is used to
        clean up the extension state."""
        # Resetting the state.
        if self._loading_manager:
            self._loading_manager.on_shutdown()
            self._loading_manager = None
        if self._stage_manager:
            self._stage_manager.on_shutdown()
            self._stage_manager = None
        # ADD THESE LINES FOR CUSTOM MANAGER CLEANUP
        if self._custom_manager:
            self._custom_manager.on_shutdown()
            self._custom_manager = None
        # Stop API server and cleanup capture resources
        asyncio.ensure_future(stop_api_server())
        get_cctv_capture().shutdown()
        # Shut down robot controller
        if self._robot_controller:
            self._robot_controller.shutdown()
            self._robot_controller = None