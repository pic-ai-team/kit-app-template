# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

import asyncio
import math
import os
import uuid
from typing import Dict, Any, Optional, Tuple

import carb
import carb.events
from carb.eventdispatcher import get_eventdispatcher
import omni.kit.app
import omni.kit.livestream.messaging as messaging
from omni.timeline import get_timeline_interface

from .viewport_capture import ViewportCapture
from .agent_client import AgentClient, AgentAction, ChatRequest, AgentResponse
from .camera_navigation import get_camera_navigation, CameraNavigation
from .usd_spawner import UsdSpawner
from .fire_incident_manager import FireIncidentManager
from .robots.robot_controller import RobotController, get_robot_controller


class CustomMessageManager:
    """Manages custom messages between web client and Kit application"""

    # Camera movement detection threshold (in scene units)
    CAMERA_MOVEMENT_THRESHOLD = 1.0

    def __init__(self, agent_backend_url: str = "http://localhost:8000"):
        """Initialize the custom message manager"""
        self._subscriptions = []
        self._timeline = get_timeline_interface()
        self._viewport_capture = ViewportCapture()
        self._agent_client = AgentClient(base_url=agent_backend_url)
        self._usd_spawner = UsdSpawner()
        self._fire_manager = FireIncidentManager()
        self._robot_controller = get_robot_controller()
        self._pending_requests: Dict[str, Dict[str, Any]] = {}  # Track pending chat requests

        # Camera position tracking (per session)
        self._last_camera_positions: Dict[str, Dict[str, float]] = {}

        # Camera navigation for moving to store locations
        self._camera_navigation: CameraNavigation = get_camera_navigation()

        # Navigation shortcuts (separate from nav positions — persisted with thumbnails)
        self._shortcuts: Dict[str, Dict[str, Any]] = {}
        self._shortcuts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shortcuts")
        self._shortcuts_file = os.path.join(self._shortcuts_dir, "shortcuts.json")
        self._shortcuts_thumbnails_dir = os.path.join(self._shortcuts_dir, "thumbnails")
        os.makedirs(self._shortcuts_thumbnails_dir, exist_ok=True)
        self._load_shortcuts()

        carb.log_info("[CustomMessageManager] Initializing...")

        # ===== REGISTER OUTGOING MESSAGES (Kit -> Web Client) =====
        outgoing_messages = [
            "customActionResult",       # Response to custom action requests
            "dataUpdateNotification",   # Notify client of data changes
            "parameterChanged",         # Confirm parameter changes
            "timelineStatusResponse",   # Timeline/simulation status response
            # Chat-related messages
            "chatResponse",             # Chat response from agent
            "chatTyping",               # Typing indicator
            "chatError",                # Chat error notification
            # Planogram
            "planogramCaptureResponse",  # Small thumbnail after frame capture
            "analyzeShelfResponse",      # Combined vision + row-detection planogram
            "automaticShelfAnalysisResponse",  # Automatic shelf analysis (route-based)
            # Camera nav position registry
            "navPositionsResponse",      # Full positions list (after any CRUD operation)
            "cameraPositionResponse",    # Current camera position query result
            "navRoutesResponse",         # Waypoint routes list
            # Fire incident simulation
            "fireAlert",                 # Broadcast when a fire incident is triggered
            "fireCleared",               # Broadcast when a fire incident is extinguished
            "fireIncidentResponse",      # Direct reply to fireIncidentRequest
            "fireAdjustResponse",        # Reply to fireAdjustRequest
            # Robot
            "robotStatusResponse",       # Robot status (position, state, nav mesh info)
            "robotCommandResponse",      # Ack for direct input commands
            "robotNavPositionsResponse", # Robot nav positions list
            "robotRoutesResponse",       # Robot routes list
            "robotCaptureResponse",      # Robot camera snapshot thumbnail
            "robotStatusUpdate",         # Periodic status push while moving
            "robotGridDataResponse",     # Nav mesh grid data for visualization
            # Navigation shortcuts
            "shortcutsResponse",         # Full shortcut list (after any CRUD operation)
            "shortcutThumbnail",          # Per-shortcut thumbnail (key + base64 JPEG)
            "shortcutClickResponse",      # Reply after clicking / navigating to a shortcut
        ]

        for message_type in outgoing_messages:
            messaging.register_event_type_to_send(message_type)
            omni.kit.app.register_event_alias(
                carb.events.type_from_string(message_type),
                message_type,
            )

        # ===== REGISTER INCOMING MESSAGE HANDLERS (Web Client -> Kit) =====
        incoming_handlers = {
            'customActionRequest': self._on_custom_action_request,
            'setParameter': self._on_set_parameter,
            'getCustomData': self._on_get_custom_data,
            'getTimelineStatus': self._on_get_timeline_status,
            'timelineControl': self._on_timeline_control,
            # Chat-related handlers
            'chatMessage': self._on_chat_message,
            'chatCancel': self._on_chat_cancel,
            # Planogram
            'planogramCaptureRequest': self._on_planogram_capture_request,
            'analyzeShelfRequest':     self._on_analyze_shelf_request,
            'automaticShelfAnalysisRequest': self._on_automatic_shelf_analysis_request,
            # Camera nav position registry
            'getNavPositions':     self._on_get_nav_positions,
            'registerNavPosition': self._on_register_nav_position,
            'deleteNavPosition':   self._on_delete_nav_position,
            'clearNavPositions':   self._on_clear_nav_positions,
            'getCameraPosition':   self._on_get_camera_position,
            'navigateTo':          self._on_navigate_to_direct,
            # Per-store built-in preset management
            'setActiveStore':      self._on_set_active_store,
            'promoteNavPosition':  self._on_promote_nav_position,
            'saveAllAsBuiltin':    self._on_save_all_as_builtin,
            # Waypoint route management
            'saveNavRoute':        self._on_save_nav_route,
            'deleteNavRoute':      self._on_delete_nav_route,
            'getNavRoutes':        self._on_get_nav_routes,
            # Fire incident simulation
            'fireIncidentRequest':  self._on_fire_incident_request,
            'fireAdjustRequest':    self._on_fire_adjust_request,
            # Robot
            'robotCommand':             self._on_robot_command,
            'robotNavigateToPoint':     self._on_robot_navigate_to_point,
            'robotNavigateRoute':       self._on_robot_navigate_route,
            'robotStop':                self._on_robot_stop,
            'robotReset':               self._on_robot_reset,
            'robotGetStatus':           self._on_robot_get_status,
            'robotBuildNavMesh':        self._on_robot_build_nav_mesh,
            'robotCaptureFrame':        self._on_robot_capture_frame,
            'robotGetNavPositions':     self._on_robot_get_nav_positions,
            'robotGetRoutes':           self._on_robot_get_routes,
            'robotGetGridData':         self._on_robot_get_grid_data,
            # Navigation shortcuts
            'getShortcuts':              self._on_get_shortcuts,
            'registerShortcut':          self._on_register_shortcut,
            'deleteShortcut':            self._on_delete_shortcut,
            'clickShortcut':             self._on_click_shortcut,
            'clearShortcuts':            self._on_clear_shortcuts,
            # Joystick camera control
            'setCameraSpeed':           self._on_set_camera_speed,
            'joystickMove':             self._on_joystick_move,
            'joystickLook':             self._on_joystick_look,
        }

        ed = get_eventdispatcher()
        for event_type, handler in incoming_handlers.items():
            # Register event alias for backward compatibility
            omni.kit.app.register_event_alias(
                carb.events.type_from_string(event_type),
                event_type,
            )
            # Subscribe to the event
            self._subscriptions.append(
                ed.observe_event(
                    observer_name=f"CustomMessageManager:{event_type}",
                    event_name=event_type,
                    on_event=handler
                )
            )

        carb.log_info("[CustomMessageManager] Initialized successfully")

    def _on_custom_action_request(self, event: carb.events.IEvent):
        """Handle custom action requests from web client"""
        payload = event.payload
        carb.log_info(f"[CustomMessageManager] Received custom action request: {payload}")

        action_type = payload.get('action_type', '')
        parameters = payload.get('parameters', {})

        # Process the action based on type
        if action_type == "rotate_camera":
            angle = parameters.get('angle', 0)
            result = {"rotated": True, "angle": angle}
        elif action_type == "toggle_feature":
            feature_name = parameters.get('feature', '')
            enabled = parameters.get('enabled', False)
            result = {"feature": feature_name, "enabled": enabled}
        else:
            result = {"error": f"Unknown action: {action_type}"}

        # Send response back to web client
        get_eventdispatcher().dispatch_event(
            "customActionResult",
            payload={
                'action_type': action_type,
                'result': result,
                'status': 'success'
            }
        )

    def _on_set_parameter(self, event: carb.events.IEvent):
        """Handle parameter setting requests"""
        payload = event.payload
        param_name = payload.get('name', '')
        param_value = payload.get('value')

        carb.log_info(f"[CustomMessageManager] Setting parameter: {param_name} = {param_value}")

        # Store in settings (example)
        if param_name and param_value is not None:
            settings = carb.settings.get_settings()
            settings.set(f"/ext/custom/{param_name}", param_value)

            # Send confirmation to web client
            get_eventdispatcher().dispatch_event(
                "parameterChanged",
                payload={
                    'name': param_name,
                    'value': param_value,
                    'status': 'success'
                }
            )

    def _on_get_custom_data(self, event: carb.events.IEvent):
        """Handle data requests from web client"""
        payload = event.payload
        data_type = payload.get('type', 'all')

        carb.log_info(f"[CustomMessageManager] Data request for type: {data_type}")

        # Collect the requested data
        if data_type == "viewport_info":
            data = {
                "resolution": "1920x1080",
                "fps": 60,
                "renderer": "RTX",
            }
        elif data_type == "app_status":
            data = {
                "version": "1.0.0",
                "uptime": "00:15:30",
                "memory_usage": "2.5GB",
            }
        else:
            data = {"message": f"No data available for type: {data_type}"}

        # Send data to web client
        get_eventdispatcher().dispatch_event(
            "dataUpdateNotification",
            payload={
                'type': data_type,
                'data': data,
            }
        )

    def _on_get_timeline_status(self, event: carb.events.IEvent):
        """Handle timeline status requests from web client"""
        carb.log_info("[CustomMessageManager] Timeline status requested")

        # Get current timeline state
        is_playing = self._timeline.is_playing()
        is_stopped = self._timeline.is_stopped()
        current_time = self._timeline.get_current_time()
        start_time = self._timeline.get_start_time()
        end_time = self._timeline.get_end_time()

        # Determine the mode
        if is_playing:
            mode = "playing"  # Scripted mode / simulation running
        elif is_stopped:
            mode = "stopped"  # Idle / not in simulation
        else:
            mode = "paused"   # Paused state

        # Send status back to web client
        get_eventdispatcher().dispatch_event(
            "timelineStatusResponse",
            payload={
                'mode': mode,
                'is_playing': is_playing,
                'is_stopped': is_stopped,
                'current_time': current_time,
                'start_time': start_time,
                'end_time': end_time,
                'scripted_mode_active': is_playing,  # True when simulation is running
            }
        )

    def _on_timeline_control(self, event: carb.events.IEvent):
        """Handle timeline control requests from web client (play/pause/stop)"""
        payload = event.payload
        action = payload.get('action', '')

        carb.log_info(f"[CustomMessageManager] Timeline control: {action}")

        result = {"action": action, "success": False, "error": None}

        try:
            if action == "play":
                self._timeline.play()
                result["success"] = True
                result["message"] = "Simulation started (scripted mode active)"
            elif action == "pause":
                self._timeline.pause()
                result["success"] = True
                result["message"] = "Simulation paused"
            elif action == "stop":
                self._timeline.stop()
                result["success"] = True
                result["message"] = "Simulation stopped (idle mode)"
            else:
                result["error"] = f"Unknown action: {action}"
        except Exception as e:
            result["error"] = str(e)
            carb.log_error(f"[CustomMessageManager] Timeline control error: {e}")

        # Send result back to web client
        get_eventdispatcher().dispatch_event(
            "timelineStatusResponse",
            payload={
                'action_result': result,
                'mode': "playing" if self._timeline.is_playing() else ("stopped" if self._timeline.is_stopped() else "paused"),
                'is_playing': self._timeline.is_playing(),
                'scripted_mode_active': self._timeline.is_playing(),
            }
        )

    # ===== CHAT MESSAGE HANDLERS =====

    def _on_chat_message(self, event: carb.events.IEvent):
        """Handle incoming chat messages from web client"""
        payload = event.payload
        message = payload.get('message', '')
        session_id = payload.get('session_id', str(uuid.uuid4()))
        request_id = payload.get('request_id', str(uuid.uuid4()))
        context = payload.get('context', {})
        language = payload.get('language') or context.get('language') or 'en'

        carb.log_info(f"[CustomMessageManager] Chat message received: {message[:50]}...")

        # Send typing indicator
        self._send_typing_indicator(session_id, True)

        # Store the pending request
        self._pending_requests[request_id] = {
            'message': message,
            'session_id': session_id,
            'context': context,
            'language': language,
            'cancelled': False
        }

        # Process chat asynchronously
        asyncio.ensure_future(
            self._process_chat_message(request_id, message, session_id, context, language)
        )

    def _on_chat_cancel(self, event: carb.events.IEvent):
        """Handle chat cancellation requests"""
        payload = event.payload
        request_id = payload.get('request_id', '')

        if request_id in self._pending_requests:
            self._pending_requests[request_id]['cancelled'] = True
            carb.log_info(f"[CustomMessageManager] Chat request cancelled: {request_id}")

    async def _process_chat_message(
        self,
        request_id: str,
        message: str,
        session_id: str,
        context: Dict[str, Any],
        language: str,
    ):
        """Process a chat message through the agent backend"""
        try:
            # Check if cancelled
            if self._is_request_cancelled(request_id):
                return

            # Get current camera position and detect movement
            current_camera_pos = self._get_camera_position()
            camera_moved, move_distance = self._detect_camera_movement(
                session_id, current_camera_pos
            )

            # Build enriched context with camera information
            enriched_context = {
                **context,
                "camera": {
                    "position": current_camera_pos,
                    "has_moved": camera_moved,
                    "move_distance": move_distance,
                }
            }

            # Update stored camera position for next comparison
            self._update_camera_position(session_id, current_camera_pos)

            carb.log_info(
                f"[CustomMessageManager] Camera context - "
                f"position: {current_camera_pos}, moved: {camera_moved}"
            )

            # Send initial message to agent with enriched context
            chat_request = ChatRequest(
                message=message,
                session_id=session_id,
                context=enriched_context,
                language=language,
            )

            response = await self._agent_client.send_chat_message(chat_request)

            # Check if cancelled
            if self._is_request_cancelled(request_id):
                return

            # Handle agent actions
            if response.action == AgentAction.CAPTURE_FRAME:
                # Agent requested frame capture for visual analysis
                await self._handle_capture_frame_action(
                    request_id=request_id,
                    original_message=message,
                    session_id=session_id,
                    action_params=response.action_params or {},
                    context=enriched_context,
                    language=language,
                )
            elif response.action == AgentAction.GET_SCENE_INFO:
                # Agent requested scene information
                await self._handle_get_scene_info_action(
                    request_id=request_id,
                    original_message=message,
                    session_id=session_id,
                    response=response,
                    context=enriched_context,
                    language=language,
                )
            elif response.action == AgentAction.NAVIGATE_TO:
                # Agent requested camera navigation to a location
                await self._handle_navigate_to_action(
                    request_id=request_id,
                    session_id=session_id,
                    response=response,
                    action_params=response.action_params or {}
                )
            elif response.action == AgentAction.FORECAST_DEMAND:
                # Demand forecast action - send status and response
                self._send_typing_indicator(
                    session_id=session_id,
                    is_typing=False,
                    agent_type='demand_forecast',
                    agent_name='Demand Forecast Agent'
                )
                self._send_chat_response(
                    session_id=session_id,
                    request_id=request_id,
                    message=response.message,
                    metadata=response.metadata
                )
            elif response.action == AgentAction.SEARCH_EC:
                # E-commerce search action - send status and response
                self._send_typing_indicator(
                    session_id=session_id,
                    is_typing=False,
                    agent_type='ec_search',
                    agent_name='E-Commerce Search Agent'
                )
                self._send_chat_response(
                    session_id=session_id,
                    request_id=request_id,
                    message=response.message,
                    metadata=response.metadata
                )
            elif response.action == AgentAction.SPAWN_USD:
                # Agent wants to spawn a USD asset — tell the browser to enter
                # spawn-mode so the user can click a location in the viewport.
                params = response.action_params or {}
                self._send_chat_response(
                    session_id=session_id,
                    request_id=request_id,
                    message=response.message,
                    metadata={
                        **(response.metadata or {}),
                        "action":     "spawn_mode",
                        "usd_path":   params.get("usd_path", ""),
                        "asset_name": params.get("asset_name", "Asset"),
                    }
                )
            else:
                # No special action, send response to client
                self._send_chat_response(
                    session_id=session_id,
                    request_id=request_id,
                    message=response.message,
                    metadata=response.metadata
                )

        except Exception as e:
            carb.log_error(f"[CustomMessageManager] Chat processing error: {e}")
            self._send_chat_error(session_id, request_id, str(e))

        finally:
            # Clean up pending request
            self._pending_requests.pop(request_id, None)
            self._send_typing_indicator(session_id, False)

    async def _handle_capture_frame_action(
        self,
        request_id: str,
        original_message: str,
        session_id: str,
        action_params: Dict[str, Any],
        context: Dict[str, Any],
        language: str,
    ):
        """Handle the capture_frame action from agent"""
        carb.log_info("[CustomMessageManager] Capturing viewport frame for analysis...")

        # Get capture parameters
        width = action_params.get('width', 1280)
        height = action_params.get('height', 720)
        followup_intent = action_params.get('followup_intent')  # e.g., 'demand_forecast', 'ec_search'
        # Use the intent-classifier's resolved_query (self-contained rewrite) so the
        # vision agent receives a fully contextualised question.
        resolved_query = action_params.get('resolved_query', original_message)

        # Capture the viewport
        frame_data = await self._viewport_capture.capture_frame_async(
            width=width,
            height=height
        )

        if self._is_request_cancelled(request_id):
            return

        if frame_data is None:
            self._send_chat_response(
                session_id=session_id,
                request_id=request_id,
                message="I couldn't capture the current view. Please try again.",
                metadata={"error": "frame_capture_failed"}
            )
            return

        # If there's a followup_intent (demand_forecast, ec_search), send back to /api/chat
        # with the frame data so it can be processed by the proper handler
        if followup_intent:
            carb.log_info(f"[CustomMessageManager] Followup intent: {followup_intent}, sending to /api/chat with frame...")

            # Send back to chat endpoint with frame data (use resolved_query)
            chat_request = ChatRequest(
                message=resolved_query,
                session_id=session_id,
                frame_data=frame_data,
                context=context,
                language=language,
            )

            response = await self._agent_client.send_chat_message(chat_request)

            if self._is_request_cancelled(request_id):
                return

            # Send the response
            self._send_chat_response(
                session_id=session_id,
                request_id=request_id,
                message=response.message,
                metadata=response.metadata,
                reasoning=response.reasoning
            )
            return

        # Standard flow: Send frame to vision agent for analysis
        carb.log_info("[CustomMessageManager] Sending frame to vision agent...")

        analysis_response = await self._agent_client.send_frame_for_analysis(
            frame_data=frame_data,
            original_query=resolved_query,
            session_id=session_id,
            context=context,
            language=language,
        )

        if self._is_request_cancelled(request_id):
            return

        # Check if image_url is available (from LuminiOne cloud upload)
        # If so, don't send the large base64 captured_frame to reduce payload size
        response_metadata = analysis_response.metadata or {}
        image_url = response_metadata.get('image_url')

        # Only include captured_frame if no image_url is available (local setup)
        # This avoids sending huge base64 payloads when we have a URL
        frame_to_send = None
        if not image_url:
            frame_to_send = analysis_response.captured_frame or frame_data
            carb.log_info("[CustomMessageManager] No image_url, sending base64 captured_frame")
        else:
            carb.log_info(f"[CustomMessageManager] Using image_url instead of base64: {image_url}")

        # Send final response to client
        self._send_chat_response(
            session_id=session_id,
            request_id=request_id,
            message=analysis_response.message,
            metadata={
                **response_metadata,
                "frame_analyzed": True
            },
            reasoning=analysis_response.reasoning,
            captured_frame=frame_to_send
        )

    async def _handle_get_scene_info_action(
        self,
        request_id: str,
        original_message: str,
        session_id: str,
        response: AgentResponse,
        context: Dict[str, Any],
        language: str,
    ):
        """Handle the get_scene_info action from agent"""
        # Gather scene information
        scene_info = self._get_scene_info()

        # Send scene info back to agent for continued processing
        updated_context = {
            **context,
            "scene_info": scene_info
        }

        chat_request = ChatRequest(
            message=original_message,
            session_id=session_id,
            context=updated_context,
            language=language,
        )

        followup_response = await self._agent_client.send_chat_message(chat_request)

        if self._is_request_cancelled(request_id):
            return

        self._send_chat_response(
            session_id=session_id,
            request_id=request_id,
            message=followup_response.message,
            metadata=followup_response.metadata
        )

    def _get_scene_info(self) -> Dict[str, Any]:
        """Get current scene information"""
        try:
            import omni.usd
            stage = omni.usd.get_context().get_stage()

            if stage is None:
                return {"error": "No stage loaded"}

            # Gather basic scene info
            root_layer = stage.GetRootLayer()
            prims = list(stage.TraverseAll())

            return {
                "root_layer": root_layer.identifier if root_layer else None,
                "prim_count": len(prims),
                "up_axis": str(stage.GetMetadata("upAxis")),
                "meters_per_unit": stage.GetMetadata("metersPerUnit"),
            }

        except Exception as e:
            carb.log_error(f"[CustomMessageManager] Failed to get scene info: {e}")
            return {"error": str(e)}

    # ===== CAMERA NAVIGATION =====

    async def _handle_navigate_to_action(
        self,
        request_id: str,
        session_id: str,
        response: AgentResponse,
        action_params: Dict[str, Any]
    ):
        """Handle the navigate_to action from agent - move camera to a location"""
        destination = action_params.get('destination', '')
        speed = action_params.get('speed', 1.0)
        instant = action_params.get('instant', False)

        carb.log_info(f"[CustomMessageManager] Navigate to: {destination} (speed={speed}, instant={instant})")

        if not destination:
            self._send_chat_response(
                session_id=session_id,
                request_id=request_id,
                message=response.message or "I need a destination to navigate to.",
                metadata={"error": "no_destination"}
            )
            return

        # Send the agent's message first (e.g., "Taking you to the Pringles section...")
        if response.message:
            self._send_chat_response(
                session_id=session_id,
                request_id=request_id,
                message=response.message,
                metadata={
                    **(response.metadata or {}),
                    "navigating_to": destination,
                    "navigation_started": True
                }
            )

        # Perform the navigation
        success = await self._camera_navigation.navigate_to(
            destination=destination,
            speed=speed,
            instant=instant
        )

        if success:
            carb.log_info(f"[CustomMessageManager] Navigation complete: {destination}")
            # Optionally send arrival notification
            # self._send_navigation_arrived(session_id, destination)
        else:
            carb.log_warn(f"[CustomMessageManager] Navigation failed: {destination}")
            # Send error message if navigation failed
            available = list(self._camera_navigation.get_positions().keys())
            self._send_chat_response(
                session_id=session_id,
                request_id=request_id,
                message=f"Sorry, I couldn't navigate to '{destination}'. Available locations: {', '.join(available[:10])}",
                metadata={"error": "navigation_failed", "destination": destination}
            )

    def get_available_locations(self) -> Dict[str, Dict[str, Any]]:
        """Get all available navigation locations"""
        return self._camera_navigation.get_positions()

    # ===== CAMERA TRACKING =====

    def _get_camera_position(self) -> Optional[Dict[str, float]]:
        """Get current camera position from viewport capture utility."""
        camera_info = self._viewport_capture.get_camera_info()
        if camera_info and camera_info.get("valid") and camera_info.get("position"):
            return camera_info["position"]
        return None

    def _calculate_camera_distance(
        self,
        pos1: Dict[str, float],
        pos2: Dict[str, float]
    ) -> float:
        """Calculate Euclidean distance between two camera positions."""
        dx = pos1.get("x", 0) - pos2.get("x", 0)
        dy = pos1.get("y", 0) - pos2.get("y", 0)
        dz = pos1.get("z", 0) - pos2.get("z", 0)
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def _detect_camera_movement(
        self,
        session_id: str,
        current_position: Optional[Dict[str, float]]
    ) -> Tuple[bool, Optional[float]]:
        """
        Detect if camera has moved since last message.

        Returns:
            Tuple of (has_moved: bool, distance: Optional[float])
        """
        if current_position is None:
            return False, None

        last_position = self._last_camera_positions.get(session_id)
        if last_position is None:
            # First message in session, no movement to detect
            return False, None

        distance = self._calculate_camera_distance(current_position, last_position)
        has_moved = distance > self.CAMERA_MOVEMENT_THRESHOLD

        if has_moved:
            carb.log_info(
                f"[CustomMessageManager] Camera moved {distance:.2f} units "
                f"(threshold: {self.CAMERA_MOVEMENT_THRESHOLD})"
            )

        return has_moved, distance

    def _update_camera_position(
        self,
        session_id: str,
        position: Optional[Dict[str, float]]
    ):
        """Store the current camera position for a session."""
        if position:
            self._last_camera_positions[session_id] = position

    def _is_request_cancelled(self, request_id: str) -> bool:
        """Check if a request has been cancelled"""
        request = self._pending_requests.get(request_id)
        return request is None or request.get('cancelled', False)

    def _send_typing_indicator(
        self,
        session_id: str,
        is_typing: bool,
        agent_type: Optional[str] = None,
        agent_name: Optional[str] = None
    ):
        """Send typing indicator with optional agent status to web client"""
        payload = {
            'session_id': session_id,
            'is_typing': is_typing
        }

        # Add agent status information if provided
        if agent_type:
            payload['agent_type'] = agent_type
        if agent_name:
            payload['agent_name'] = agent_name

        get_eventdispatcher().dispatch_event("chatTyping", payload=payload)

    def _send_chat_response(
        self,
        session_id: str,
        request_id: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
        reasoning: Optional[str] = None,
        captured_frame: Optional[str] = None,
        image_url: Optional[str] = None
    ):
        """Send chat response to web client"""
        payload = {
            'session_id': session_id,
            'request_id': request_id,
            'message': message,
            'metadata': metadata or {},
            'status': 'success'
        }

        # Add optional fields if present
        if reasoning:
            payload['reasoning'] = reasoning
        if captured_frame:
            payload['captured_frame'] = captured_frame
            carb.log_info(f"[CustomMessageManager] Including captured frame ({len(captured_frame)} chars)")

        # Add image_url as top-level field for frontend visualization
        # Also extract from metadata if not explicitly provided
        final_image_url = image_url or (metadata.get('image_url') if metadata else None)
        if final_image_url:
            payload['image_url'] = final_image_url
            carb.log_info(f"[CustomMessageManager] Including image URL: {final_image_url}")

        get_eventdispatcher().dispatch_event("chatResponse", payload=payload)
        carb.log_info(f"[CustomMessageManager] Chat response sent: {message[:50]}...")

    def _send_chat_error(self, session_id: str, request_id: str, error: str):
        """Send chat error to web client"""
        get_eventdispatcher().dispatch_event(
            "chatError",
            payload={
                'session_id': session_id,
                'request_id': request_id,
                'error': error,
                'status': 'error'
            }
        )
        carb.log_error(f"[CustomMessageManager] Chat error: {error}")

    # ===== PLANOGRAM CAPTURE =====
    # The full viewport frame is too large (~1-4 MB) for the WebRTC data channel
    # (~64 KB limit).  Strategy:
    #   1. planogramCaptureRequest  → Kit captures, stores full JPEG in memory,
    #                                 sends a small thumbnail (~15 KB) to browser.
    # The large image never travels over WebRTC.

    def _on_planogram_capture_request(self, event: carb.events.IEvent):
        """Handle planogramCaptureRequest — capture and store frame, send thumbnail."""
        carb.log_info("[CustomMessageManager] planogramCaptureRequest received")
        asyncio.ensure_future(self._capture_and_send_planogram_frame())

    async def _capture_and_send_planogram_frame(self):
        """Capture viewport, compress to JPEG, store full-res, send small thumbnail."""
        try:
            frame_b64 = await self._viewport_capture.capture_frame_async()
            if frame_b64 is None:
                get_eventdispatcher().dispatch_event(
                    "planogramCaptureResponse",
                    payload={"success": False, "error": "Frame capture failed"},
                )
                carb.log_warn("[CustomMessageManager] Planogram capture returned no data")
                return

            thumbnail_b64, vision_b64 = self._compress_planogram_frame(frame_b64)
            self._planogram_frame: Optional[str] = vision_b64

            carb.log_info(
                f"[CustomMessageManager] Planogram captured — "
                f"vision={len(vision_b64)//1024} KB, thumb={len(thumbnail_b64)//1024} KB"
            )

            get_eventdispatcher().dispatch_event(
                "planogramCaptureResponse",
                payload={"success": True, "thumbnail": thumbnail_b64},
            )
        except Exception as exc:
            carb.log_error(f"[CustomMessageManager] Planogram capture error: {exc}")
            get_eventdispatcher().dispatch_event(
                "planogramCaptureResponse",
                payload={"success": False, "error": str(exc)},
            )

    @staticmethod
    def _compress_planogram_frame(frame_b64: str):
        """
        Return (thumbnail_b64, vision_b64).
        thumbnail  ~15 KB (250 px wide, q50) — safe for WebRTC data channel
        vision     full-res q90 JPEG         — sent server-side only
        """
        import base64 as _b64
        import io as _io
        try:
            from PIL import Image as _Image
            raw = _b64.b64decode(frame_b64)
            img = _Image.open(_io.BytesIO(raw)).convert("RGB")

            vis_buf = _io.BytesIO()
            img.save(vis_buf, format="JPEG", quality=90)
            vision_b64 = _b64.b64encode(vis_buf.getvalue()).decode("utf-8")

            ratio = 250 / img.width if img.width > 250 else 1.0
            thumb = img.resize((int(img.width * ratio), int(img.height * ratio)), _Image.LANCZOS)
            th_buf = _io.BytesIO()
            thumb.save(th_buf, format="JPEG", quality=50)
            thumbnail_b64 = _b64.b64encode(th_buf.getvalue()).decode("utf-8")

            return thumbnail_b64, vision_b64
        except Exception as exc:
            carb.log_warn(f"[CustomMessageManager] Frame compression failed ({exc}), using raw")
            return frame_b64, frame_b64

    # ===== ANALYZE SHELF (vision product ID + row detection planogram) =====

    def _on_analyze_shelf_request(self, event: carb.events.IEvent):
        """Handle analyzeShelfRequest — capture frame, identify products, detect rows."""
        carb.log_info("[CustomMessageManager] analyzeShelfRequest received")
        asyncio.ensure_future(self._run_shelf_analysis(dict(event.payload)))

    async def _run_shelf_analysis(self, payload: dict):
        """
        Full pipeline:
        1. Capture viewport frame
        2. POST to /api/identify-shelf-products → list of asset_keys + product_info
        3. For each key, call usd_spawner.detect_rows_for_key()
        4. Merge per-product rows into unified shelf levels by floor_z
        5. Enrich with stock / initial_stock from product_info
        6. Dispatch analyzeShelfResponse
        """
        import json as _json
        import urllib.request as _urllib

        tolerance  = float(payload.get("tolerance_cm", 8.0))
        model      = payload.get("model", "qwen")
        asset_keys = payload.get("asset_keys")  # pre-filled → skip vision (refresh mode)
        product_info: dict = payload.get("product_info") or {}

        def _err(msg: str):
            get_eventdispatcher().dispatch_event(
                "analyzeShelfResponse", payload={"success": False, "error": msg}
            )

        # 1. Capture frame (always — need a fresh thumbnail)
        frame_b64 = await self._viewport_capture.capture_frame_async()
        if not frame_b64:
            _err("Frame capture failed")
            return

        thumbnail_b64, vision_b64 = self._compress_planogram_frame(frame_b64)

        # 2. Identify products (skipped in refresh mode when asset_keys already known)
        if not asset_keys:
            backend = self._agent_client._base_url
            url     = f"{backend}/api/identify-shelf-products"
            body    = _json.dumps({"frame_data": vision_b64, "model": model}).encode("utf-8")
            try:
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: _json.loads(
                        _urllib.urlopen(
                            _urllib.Request(url, data=body,
                                            headers={"Content-Type": "application/json"},
                                            method="POST"),
                            timeout=120,
                        ).read().decode("utf-8")
                    ),
                )
                asset_keys = resp.get("products", [])
                product_info = resp.get("product_info", {})
            except Exception as exc:
                carb.log_error(f"[CustomMessageManager] identify-shelf-products failed: {exc}")
                _err(f"Product identification failed: {exc}")
                return

            if not asset_keys:
                _err("No products recognised in the captured frame")
                return

        carb.log_info(f"[CustomMessageManager] analyzeShelf ({'refresh' if payload.get('asset_keys') else 'full'}): {len(asset_keys)} products → {asset_keys}")

        # 3. Row detection per product
        all_rows: dict = {}
        for key in asset_keys:
            result = self._usd_spawner.detect_rows_for_key(key, tolerance)
            if result and result.get("rows"):
                all_rows[key] = result["rows"]
                carb.log_info(f"[CustomMessageManager] analyzeShelf: {key} → {len(result['rows'])} rows")

        if not all_rows:
            _err("No shelf rows detected for any identified product")
            return

        # 4. Merge into unified shelf levels aligned by floor_z
        shelf_levels = self._merge_shelf_levels(all_rows, tolerance)

        # 5. Enrich with stock / initial_stock from product_info
        product_shelf_count: dict = {}
        for lv in shelf_levels:
            for key in lv.get("products", {}):
                product_shelf_count[key] = product_shelf_count.get(key, 0) + 1

        total_stock = 0
        total_initial_stock = 0

        for lv in shelf_levels:
            shelf_stock = 0
            shelf_init = 0
            for key, prod_data in lv.get("products", {}).items():
                current_count = prod_data.get("count", 0)
                info = product_info.get(key, {})
                product_init_stock = info.get("initial_stock", 0) or 0
                num_shelves = product_shelf_count.get(key, 1)
                shelf_init_stock = round(product_init_stock / num_shelves) if num_shelves > 0 else 0

                prod_data["stock"] = current_count
                prod_data["initial_stock"] = shelf_init_stock
                prod_data.pop("prim_paths", None)

                shelf_stock += current_count
                shelf_init += shelf_init_stock

            lv["shelf_stock_level"] = round(shelf_stock / shelf_init, 2) if shelf_init > 0 else 0.0
            total_stock += shelf_stock
            total_initial_stock += shelf_init

        # 6. Respond
        get_eventdispatcher().dispatch_event(
            "analyzeShelfResponse",
            payload={
                "success":      True,
                "shelf_levels": shelf_levels,
                "asset_keys":   list(all_rows.keys()),
                "thumbnail":    thumbnail_b64,
                "tolerance_cm": tolerance,
                "stock":        total_stock,
                "initial_stock": total_initial_stock,
                "stock_level":  round(total_stock / total_initial_stock, 2) if total_initial_stock > 0 else 0.0,
            },
        )

    @staticmethod
    def _merge_shelf_levels(all_rows: dict, tolerance: float) -> list:
        """
        Align per-product rows into shared shelf levels by floor_z proximity.
        Returns [{level, floor_z, products: {asset_key: {count, prim_paths}}}]
        """
        entries = sorted(
            [(row["floor_z"], key, row)
             for key, rows in all_rows.items()
             for row in rows],
            key=lambda x: x[0],
            reverse=True,
        )
        if not entries:
            return []

        # Cluster by floor_z gap (2× tolerance since products on the same shelf
        # level may not share exactly the same Z origin)
        clusters: list = []
        cur = [entries[0]]
        for entry in entries[1:]:
            if abs(entry[0] - cur[-1][0]) <= tolerance * 2:
                cur.append(entry)
            else:
                clusters.append(cur)
                cur = [entry]
        clusters.append(cur)

        shelf_levels = []
        for level_num, cluster in enumerate(clusters, start=1):
            avg_z = round(sum(e[0] for e in cluster) / len(cluster), 2)
            products: dict = {}
            for _, key, row in cluster:
                if key not in products:
                    products[key] = {"count": 0, "prim_paths": []}
                products[key]["count"]      += row["prim_count"]
                products[key]["prim_paths"] += row["prim_paths"]
            shelf_levels.append({"level": level_num, "floor_z": avg_z, "products": products})

        return shelf_levels

    # ===== AUTOMATIC SHELF ANALYSIS (route-based) =====

    PLANOGRAM_ROUTE_PREFIX = "planogram_analysis_"

    def _on_automatic_shelf_analysis_request(self, event: carb.events.IEvent):
        """Handle automaticShelfAnalysisRequest — run shelf analysis along a route."""
        route_name = event.payload.get("route", "").strip()
        if not route_name:
            get_eventdispatcher().dispatch_event(
                "automaticShelfAnalysisResponse",
                payload={"success": False, "error": "No route specified"},
            )
            return
        carb.log_info(f"[CustomMessageManager] automaticShelfAnalysisRequest route={route_name}")
        asyncio.ensure_future(self._run_automatic_shelf_analysis(route_name, dict(event.payload)))

    async def _run_automatic_shelf_analysis(self, route_name: str, payload: dict) -> dict:
        """
        Automated shelf stock analysis along a planogram route.

        For each waypoint in the route:
        1. Teleport hidden camera to waypoint position
        2. Capture a frame
        3. POST to /api/identify-shelf-products → asset_keys + product_info + rack
        4. Run row detection per product (reuse _merge_shelf_levels)
        5. Compute stock ratios using initial_stock from product_info

        After all waypoints: merge results that share the same rack (majority
        vote on rack_id) and deduplicate products across merged waypoints.

        Returns list of per-rack analysis results dispatched via
        automaticShelfAnalysisResponse.
        """
        import json as _json
        import urllib.request as _urllib
        from .cctv_capture import get_cctv_capture

        tolerance = float(payload.get("tolerance_cm", 8.0))
        model = payload.get("model", "qwen")
        capture_width = int(payload.get("capture_width", 1920))
        capture_height = int(payload.get("capture_height", 1080))

        def _err(msg: str):
            get_eventdispatcher().dispatch_event(
                "automaticShelfAnalysisResponse",
                payload={"success": False, "error": msg},
            )
            return {"success": False, "error": msg}

        # Get route waypoints
        route_data = self._camera_navigation.get_all_routes().get(route_name)
        if not route_data:
            return _err(f"Route '{route_name}' not found")

        waypoints = route_data.get("waypoints", [])
        if not waypoints:
            return _err(f"Route '{route_name}' has no waypoints")

        carb.log_info(f"[AutoShelfAnalysis] Starting analysis on route '{route_name}' with {len(waypoints)} waypoint(s)")

        # Use the CCTV capture infrastructure for the hidden camera
        cctv = get_cctv_capture()
        if not cctv._ensure_cctv_camera():
            return _err("Failed to create hidden camera")
        use_hydra = cctv._ensure_hydra_texture(width=capture_width, height=capture_height)
        carb.log_info(
            f"[AutoShelfAnalysis] Capture settings: hydra={use_hydra} "
            f"requested={capture_width}x{capture_height}"
        )

        backend = self._agent_client._base_url

        # Collect per-waypoint raw results before merging
        wp_raw_results = []

        for wp_idx, wp in enumerate(waypoints):
            location = wp.get("location", [0, 0, 0])
            rotation = wp.get("rotation", [0, 0, 0])

            carb.log_info(f"[AutoShelfAnalysis] Waypoint {wp_idx + 1}/{len(waypoints)} at {location}")

            # 1. Teleport hidden camera
            if not cctv._set_cctv_camera_pose(location, rotation):
                carb.log_warn(f"[AutoShelfAnalysis] Skipping waypoint {wp_idx + 1} — pose set failed")
                continue

            await asyncio.sleep(0.15)

            # 2. Capture frame
            if use_hydra:
                frame_b64 = await cctv._capture_single_frame()
            else:
                frame_b64 = await cctv._capture_via_active_viewport()

            if not frame_b64:
                carb.log_warn(f"[AutoShelfAnalysis] Skipping waypoint {wp_idx + 1} — capture failed")
                continue

            vision_b64 = frame_b64

            # 3. Identify products via backend (enriched endpoint)
            url = f"{backend}/api/identify-shelf-products"
            body = _json.dumps({"frame_data": vision_b64, "model": model}).encode("utf-8")
            try:
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: _json.loads(
                        _urllib.urlopen(
                            _urllib.Request(url, data=body,
                                           headers={"Content-Type": "application/json"},
                                           method="POST"),
                            timeout=120,
                        ).read().decode("utf-8")
                    ),
                )
            except Exception as exc:
                carb.log_error(f"[AutoShelfAnalysis] identify-shelf-products failed at wp{wp_idx + 1}: {exc}")
                continue

            asset_keys = resp.get("products", [])
            product_info = resp.get("product_info", {})
            rack_id = resp.get("rack_id")
            rack_name = resp.get("rack_name", rack_id or "Unknown")

            if not asset_keys:
                carb.log_warn(f"[AutoShelfAnalysis] No products at waypoint {wp_idx + 1}")
                continue

            # 4. Row detection per product
            all_rows: dict = {}
            for key in asset_keys:
                result = self._usd_spawner.detect_rows_for_key(key, tolerance)
                if result and result.get("rows"):
                    all_rows[key] = result["rows"]

            if not all_rows:
                carb.log_warn(f"[AutoShelfAnalysis] No shelf rows detected at waypoint {wp_idx + 1}")
                continue

            wp_raw_results.append({
                "waypoint": wp_idx + 1,
                "location": location,
                "rotation": rotation,
                "rack_id": rack_id,
                "rack_name": rack_name,
                "all_rows": all_rows,
                "product_info": product_info,
                "asset_keys": asset_keys,
            })

        if not wp_raw_results:
            return _err("No waypoints produced results")

        # ── Merge waypoints by rack_id ──────────────────────────────────────
        # Group waypoints by rack_id. Products seen in multiple waypoints for
        # the same rack are deduplicated (same asset_key = same product).
        rack_groups: dict = {}  # rack_id → list of wp_raw dicts
        for wp_raw in wp_raw_results:
            rid = wp_raw["rack_id"] or "unknown"
            rack_groups.setdefault(rid, []).append(wp_raw)

        results = []
        for rid, group in rack_groups.items():
            # Rack name: use the name from the first waypoint
            rack_name = group[0]["rack_name"]

            # Merge all_rows across waypoints, deduplicating by asset_key.
            # Same asset_key in multiple waypoints = same products (no double-count).
            # We take the union: for each product, keep the entry with the highest count.
            merged_rows: dict = {}
            merged_product_info: dict = {}
            for wp_raw in group:
                for key, rows in wp_raw["all_rows"].items():
                    if key not in merged_rows:
                        merged_rows[key] = rows
                    # else: already counted — skip duplicate
                merged_product_info.update(wp_raw["product_info"])

            # Merge into unified shelf levels
            shelf_levels = self._merge_shelf_levels(merged_rows, tolerance)

            # Compute stock ratios
            product_shelf_count: dict = {}
            for lv in shelf_levels:
                for key in lv.get("products", {}):
                    product_shelf_count[key] = product_shelf_count.get(key, 0) + 1

            total_stock = 0
            total_initial_stock = 0

            for lv in shelf_levels:
                shelf_stock = 0
                shelf_init = 0
                for key, prod_data in lv.get("products", {}).items():
                    current_count = prod_data.get("count", 0)
                    info = merged_product_info.get(key, {})
                    product_init_stock = info.get("initial_stock", 0) or 0
                    num_shelves = product_shelf_count.get(key, 1)
                    shelf_init_stock = round(product_init_stock / num_shelves) if num_shelves > 0 else 0

                    prod_data["stock"] = current_count
                    prod_data["initial_stock"] = shelf_init_stock
                    prod_data.pop("prim_paths", None)
                    prod_data.pop("count", None)

                    shelf_stock += current_count
                    shelf_init += shelf_init_stock

                lv["shelf_stock_level"] = round(shelf_stock / shelf_init, 2) if shelf_init > 0 else 0.0
                total_stock += shelf_stock
                total_initial_stock += shelf_init

            rack_result = {
                "rack_id": rid,
                "rack_name": rack_name,
                "waypoint_count": len(group),
                "products": list(merged_rows.keys()),
                "stock_level": round(total_stock / total_initial_stock, 2) if total_initial_stock > 0 else 0.0,
                "stock": total_stock,
                "initial_stock": total_initial_stock,
                "shelf_levels": shelf_levels,
                "asset_keys": list(merged_rows.keys()),
            }
            results.append(rack_result)
            carb.log_info(
                f"[AutoShelfAnalysis] Rack {rack_name}: "
                f"stock={total_stock}/{total_initial_stock} levels={len(shelf_levels)} "
                f"(merged from {len(group)} waypoint(s))"
            )

        # Dispatch results
        get_eventdispatcher().dispatch_event(
            "automaticShelfAnalysisResponse",
            payload={
                "success": True,
                "route": route_name,
                "waypoint_count": len(waypoints),
                "results": results,
            },
        )
        carb.log_info(f"[AutoShelfAnalysis] Completed: {len(results)} racks from {len(wp_raw_results)} waypoints")
        return {"success": True, "results": results}

    # ===== CAMERA NAV POSITION REGISTRY =====

    def _dispatch_nav_positions(self, extra: dict = None) -> None:
        """Send positions list to the web client, chunked to stay under WebRTC 65535-byte limit."""
        positions = self._camera_navigation.get_all_positions_with_metadata()
        keys = list(positions.keys())
        CHUNK_SIZE = 10
        total_chunks = max(1, (len(keys) + CHUNK_SIZE - 1) // CHUNK_SIZE)

        for i in range(0, len(keys), CHUNK_SIZE):
            chunk_keys = keys[i:i + CHUNK_SIZE]
            chunk = {k: positions[k] for k in chunk_keys}
            chunk_index = i // CHUNK_SIZE
            payload = {
                "positions": chunk,
                "chunk": chunk_index,
                "total_chunks": total_chunks,
                "is_last_chunk": chunk_index == total_chunks - 1,
                **(extra or {} if chunk_index == total_chunks - 1 else {}),
            }
            get_eventdispatcher().dispatch_event("navPositionsResponse", payload=payload)

    def _on_get_nav_positions(self, event: carb.events.IEvent) -> None:
        carb.log_info("[CustomMessageManager] getNavPositions received")
        self._dispatch_nav_positions()

    def _on_register_nav_position(self, event: carb.events.IEvent) -> None:
        payload = event.payload
        name = payload.get("name", "").strip()
        location = tuple(payload.get("location", [0.0, 0.0, 0.0]))
        rotation = tuple(payload.get("rotation", [0.0, 0.0, 0.0]))
        description = payload.get("description", name)

        if not name:
            carb.log_warn("[CustomMessageManager] registerNavPosition: empty name ignored")
            return

        success = self._camera_navigation.save_position(name, location, rotation, description)
        carb.log_info(f"[CustomMessageManager] registerNavPosition '{name}' → saved={success}")

        # Sync to agent backend so chat can navigate to this position by name
        key = name.lower().strip().replace(" ", "_")
        asyncio.ensure_future(self._sync_nav_position_to_backend(
            key, list(location), list(rotation), description
        ))

        self._dispatch_nav_positions({"saved": success, "name": name})

    def _on_delete_nav_position(self, event: carb.events.IEvent) -> None:
        name = event.payload.get("name", "")
        success = self._camera_navigation.delete_position(name)
        carb.log_info(f"[CustomMessageManager] deleteNavPosition '{name}' → success={success}")

        asyncio.ensure_future(self._delete_nav_position_from_backend(name.lower()))

        self._dispatch_nav_positions({"deleted": success, "name": name})

    def _on_clear_nav_positions(self, event: carb.events.IEvent) -> None:
        success = self._camera_navigation.clear_custom_positions()
        carb.log_info(f"[CustomMessageManager] clearNavPositions → success={success}")

        asyncio.ensure_future(self._clear_nav_positions_from_backend())

        self._dispatch_nav_positions({"cleared": success})

    # ── Backend sync helpers ──────────────────────────────────────────────────

    async def _sync_nav_position_to_backend(
        self, name: str, location: list, rotation: list, description: str
    ) -> None:
        """POST a registered nav position to the agent backend."""
        import json as _json
        import urllib.request as _urllib
        url = f"{self._agent_client._base_url}/api/nav-positions"
        body = _json.dumps({
            "name": name, "description": description,
            "location": location, "rotation": rotation,
        }).encode("utf-8")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: _urllib.urlopen(
                    _urllib.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST"),
                    timeout=5
                )
            )
            carb.log_info(f"[CustomMessageManager] Synced nav position '{name}' to backend")
        except Exception as e:
            carb.log_warn(f"[CustomMessageManager] Could not sync nav position to backend: {e}")

    async def _delete_nav_position_from_backend(self, name: str) -> None:
        """DELETE a nav position from the agent backend."""
        import urllib.request as _urllib
        url = f"{self._agent_client._base_url}/api/nav-positions/{name}"
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: _urllib.urlopen(
                    _urllib.Request(url, method="DELETE"),
                    timeout=5
                )
            )
            carb.log_info(f"[CustomMessageManager] Deleted nav position '{name}' from backend")
        except Exception as e:
            carb.log_warn(f"[CustomMessageManager] Could not delete nav position from backend: {e}")

    async def _clear_nav_positions_from_backend(self) -> None:
        """DELETE all custom nav positions from the agent backend."""
        import urllib.request as _urllib
        url = f"{self._agent_client._base_url}/api/nav-positions"
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: _urllib.urlopen(
                    _urllib.Request(url, method="DELETE"),
                    timeout=5
                )
            )
            carb.log_info("[CustomMessageManager] Cleared all nav positions from backend")
        except Exception as e:
            carb.log_warn(f"[CustomMessageManager] Could not clear nav positions from backend: {e}")

    def _on_get_camera_position(self, event: carb.events.IEvent) -> None:
        try:
            pos = self._read_camera_position_robust()
            carb.log_info(f"[CustomMessageManager] getCameraPosition → {pos}")
            if pos:
                get_eventdispatcher().dispatch_event(
                    "cameraPositionResponse",
                    payload={
                        "success": True,
                        "location": pos["location"],
                        "rotation": pos["rotation"],
                    }
                )
            else:
                get_eventdispatcher().dispatch_event(
                    "cameraPositionResponse",
                    payload={"success": False, "error": "Camera position unavailable"}
                )
        except Exception as exc:
            carb.log_error(f"[CustomMessageManager] getCameraPosition error: {exc}")
            get_eventdispatcher().dispatch_event(
                "cameraPositionResponse",
                payload={"success": False, "error": str(exc)}
            )

    def _read_camera_position_robust(self):
        """
        Read camera position + rotation via world-transform matrix decomposition.
        Always decomposes the world matrix for rotation — never reads raw op values,
        which can be stale or zero-initialized if ops were just created.
        Returns dict with 'location' (list[float,3]) and 'rotation' (list[float,3]).
        """
        import math
        try:
            import omni.usd
            from pxr import UsdGeom

            stage = omni.usd.get_context().get_stage()
            if not stage:
                carb.log_warn("[CustomMessageManager] No stage for camera position")
                return None

            # Prefer the active viewport camera path so we read what the user sees
            camera_prim = None
            try:
                from omni.kit.viewport.utility import get_active_viewport_camera_path
                cam_path = get_active_viewport_camera_path()
                if cam_path:
                    camera_prim = stage.GetPrimAtPath(cam_path)
                    carb.log_info(f"[CustomMessageManager] Using viewport camera: {cam_path}")
            except Exception as e:
                carb.log_warn(f"[CustomMessageManager] Could not get viewport camera path: {e}")

            # Fall back to the configured camera path
            if not camera_prim or not camera_prim.IsValid():
                fallback = self._camera_navigation._camera_path
                camera_prim = stage.GetPrimAtPath(fallback)
                carb.log_info(f"[CustomMessageManager] Using configured camera: {fallback}")

            if not camera_prim or not camera_prim.IsValid():
                carb.log_warn("[CustomMessageManager] No valid camera prim found")
                return None

            xformable = UsdGeom.Xformable(camera_prim)

            # Always use world transform — it reflects the true camera state regardless
            # of whether ops are translate/rotate or a combined matrix op.
            world_xform = xformable.ComputeLocalToWorldTransform(0)
            t = world_xform.ExtractTranslation()
            location = [float(t[0]), float(t[1]), float(t[2])]

            # Decompose rotation matrix → Euler XYZ degrees
            m = world_xform.ExtractRotationMatrix()
            # USD uses row-vector convention: M = Rx * Ry * Rz
            # sy = |cos(ry)| comes from the first row of M
            sy = math.sqrt(m[0][0] ** 2 + m[0][1] ** 2)
            if sy > 1e-6:
                rx = math.degrees(math.atan2(m[1][2], m[2][2]))
                ry = math.degrees(math.atan2(-m[0][2], sy))
                rz = math.degrees(math.atan2(m[0][1], m[0][0]))
            else:
                rx = math.degrees(math.atan2(-m[2][1], m[1][1]))
                ry = math.degrees(math.atan2(-m[0][2], sy))
                rz = 0.0
            rotation = [rx, ry, rz]

            carb.log_info(
                f"[CustomMessageManager] Camera: "
                f"t=({location[0]:.1f},{location[1]:.1f},{location[2]:.1f}) "
                f"r=({rotation[0]:.2f},{rotation[1]:.2f},{rotation[2]:.2f})"
            )
            return {"location": location, "rotation": rotation}

        except Exception as e:
            import traceback
            carb.log_error(f"[CustomMessageManager] _read_camera_position_robust failed: {e}")
            carb.log_error(traceback.format_exc())
            return None

    def _on_navigate_to_direct(self, event: carb.events.IEvent) -> None:
        """Direct navigation from the UI panel (bypasses the chat agent)."""
        destination = event.payload.get("destination", "")
        instant = event.payload.get("instant", False)
        speed = float(event.payload.get("speed", 1.0))
        carb.log_info(f"[CustomMessageManager] navigateTo '{destination}' instant={instant}")
        asyncio.ensure_future(self._do_navigate_direct(destination, instant, speed))

    async def _do_navigate_direct(self, destination: str|Dict, instant: bool, speed: float) -> None:
        success = await self._camera_navigation.navigate_to(
            destination=destination, instant=instant, speed=speed
        )
        if not success:
            carb.log_warn(f"[CustomMessageManager] Direct navigation failed: '{destination}'")

    # ── Per-store built-in preset handlers ──────────────────────────────────

    # ── Waypoint route handlers ────────────────────────────────────────────

    def _on_save_nav_route(self, event: carb.events.IEvent) -> None:
        """
        Handle saveNavRoute message.
        Payload: {
            destination: "pringles",
            waypoints: [{location:[x,y,z], rotation:[rx,ry,rz]}, ...],
            start: {location:[x,y,z], rotation:[rx,ry,rz]}  // optional
        }
        """
        destination = event.payload.get("destination", "").strip()
        waypoints = event.payload.get("waypoints", [])
        if not destination:
            carb.log_warn("[CustomMessageManager] saveNavRoute: empty destination ignored")
            return
        # Convert flat dicts from WebRTC payload
        wp_list = []
        for wp in waypoints:
            loc = wp.get("location", [0, 0, 0])
            rot = wp.get("rotation", [0, 0, 0])
            wp_list.append({"location": list(loc), "rotation": list(rot)})

        # Optional start position
        start_raw = event.payload.get("start")
        start = None
        if start_raw and isinstance(start_raw, dict):
            start = {
                "location": list(start_raw.get("location", [0, 0, 0])),
                "rotation": list(start_raw.get("rotation", [0, 0, 0])),
            }

        success = self._camera_navigation.save_route(destination, wp_list, start=start)
        carb.log_info(
            f"[CustomMessageManager] saveNavRoute '{destination}' "
            f"({len(wp_list)} waypoints, start={'yes' if start else 'no'}) → saved={success}"
        )
        self._dispatch_nav_routes({"saved": success, "destination": destination})

    def _on_delete_nav_route(self, event: carb.events.IEvent) -> None:
        """Handle deleteNavRoute message.  Payload: { destination: "pringles" }"""
        destination = event.payload.get("destination", "").strip()
        if not destination:
            return
        success = self._camera_navigation.delete_route(destination)
        carb.log_info(f"[CustomMessageManager] deleteNavRoute '{destination}' → success={success}")
        self._dispatch_nav_routes({"deleted": success, "destination": destination})

    def _on_get_nav_routes(self, event: carb.events.IEvent) -> None:
        carb.log_info("[CustomMessageManager] getNavRoutes received")
        self._dispatch_nav_routes()

    def _dispatch_nav_routes(self, extra: dict = None) -> None:
        """Send all routes to the web client."""
        routes = self._camera_navigation.get_all_routes()
        payload = {"routes": routes, **(extra or {})}
        get_eventdispatcher().dispatch_event("navRoutesResponse", payload=payload)

    # ── End waypoint route handlers ────────────────────────────────────────

    def _on_set_active_store(self, event: carb.events.IEvent) -> None:
        """
        Handle setActiveStore message from the web client.
        Payload: { store_key: "pipc" | "711" | ... }
        Loads the store's built-in positions from store_presets/<key>.json.
        """
        store_key = event.payload.get("store_key", "").strip()
        if not store_key:
            carb.log_warn("[CustomMessageManager] setActiveStore: empty store_key ignored")
            return
        carb.log_info(f"[CustomMessageManager] setActiveStore '{store_key}'")
        self._camera_navigation.set_active_store(store_key)
        self._dispatch_nav_positions({"store_key": store_key})
        self._dispatch_nav_routes({"store_key": store_key})

    def _on_promote_nav_position(self, event: carb.events.IEvent) -> None:
        """
        Handle promoteNavPosition message — promote a single custom position to built-in
        for the currently active store.
        Payload: { name: "entrance" }
        """
        name = event.payload.get("name", "").strip()
        if not name:
            carb.log_warn("[CustomMessageManager] promoteNavPosition: empty name ignored")
            return
        carb.log_info(f"[CustomMessageManager] promoteNavPosition '{name}'")
        success = self._camera_navigation.promote_to_builtin(name)
        self._dispatch_nav_positions({"promoted": success, "name": name})

    def _on_save_all_as_builtin(self, event: carb.events.IEvent) -> None:
        """
        Handle saveAllAsBuiltin message — promote ALL current custom positions to
        built-in for the currently active store.
        Payload: {} (no parameters needed)
        """
        carb.log_info("[CustomMessageManager] saveAllAsBuiltin")
        success = self._camera_navigation.save_all_custom_as_builtin()
        self._dispatch_nav_positions({"all_saved_as_builtin": success})

    # ===== END CAMERA NAV POSITION REGISTRY =====

    # =========================================================================
    # NAVIGATION SHORTCUTS  (separate from nav positions — persisted with thumbnails)
    # =========================================================================

    def _load_shortcuts(self) -> None:
        """Load shortcuts from shortcuts.json on disk."""
        import json as _json
        try:
            if os.path.exists(self._shortcuts_file):
                with open(self._shortcuts_file, "r") as f:
                    self._shortcuts = _json.load(f)
                carb.log_info(f"[CustomMessageManager] Loaded {len(self._shortcuts)} shortcuts")
            else:
                self._shortcuts = {}
                carb.log_info("[CustomMessageManager] No shortcuts.json found, starting empty")
        except Exception as e:
            carb.log_warn(f"[CustomMessageManager] Failed to load shortcuts: {e}")
            self._shortcuts = {}

    def _save_shortcuts(self) -> None:
        """Persist shortcuts dict to shortcuts.json."""
        import json as _json
        try:
            with open(self._shortcuts_file, "w") as f:
                _json.dump(self._shortcuts, f, indent=2)
            carb.log_info(f"[CustomMessageManager] Saved {len(self._shortcuts)} shortcuts")
        except Exception as e:
            carb.log_error(f"[CustomMessageManager] Failed to save shortcuts: {e}")

    def _dispatch_shortcuts(self, extra: dict = None) -> None:
        """Send the full shortcut list to the web client."""
        shortcuts_list = []
        for key, data in self._shortcuts.items():
            raw_pos = data.get("position", [0.0, 0.0, 0.0])
            shortcuts_list.append({
                "key":      key,
                "label":    data.get("label", key),
                "position": [round(float(v), 3) for v in raw_pos],
                "type":     "navigation",
            })
        payload = {"shortcuts": shortcuts_list}
        if extra:
            payload.update(extra)
        get_eventdispatcher().dispatch_event("shortcutsResponse", payload=payload)

    def _dispatch_shortcut_thumbnails(self) -> None:
        """Send cached thumbnails for all shortcuts that have one on disk."""
        import base64 as _b64
        for key in self._shortcuts:
            thumb_path = os.path.join(self._shortcuts_thumbnails_dir, f"{key}.jpg")
            if os.path.exists(thumb_path):
                try:
                    with open(thumb_path, "rb") as f:
                        b64 = _b64.b64encode(f.read()).decode("utf-8")
                    get_eventdispatcher().dispatch_event(
                        "shortcutThumbnail",
                        payload={"key": key, "thumbnail": b64},
                    )
                except Exception as e:
                    carb.log_warn(f"[CustomMessageManager] Failed to read thumbnail for shortcut '{key}': {e}")

    def _on_get_shortcuts(self, event: carb.events.IEvent) -> None:
        carb.log_info("[CustomMessageManager] getShortcuts received")
        self._dispatch_shortcuts()
        # Send thumbnails as separate events (keeps each under WebRTC 64 KB limit)
        self._dispatch_shortcut_thumbnails()

    def _on_register_shortcut(self, event: carb.events.IEvent) -> None:
        """Register a new shortcut at the provided or current camera position and capture a thumbnail."""
        payload = event.payload or {}
        name = payload.get("name", "").strip()
        description = payload.get("description", name)
        location = payload.get("location")
        rotation = payload.get("rotation")

        if not name:
            carb.log_warn("[CustomMessageManager] registerShortcut: empty name ignored")
            return

        # Use provided position or fall back to current camera
        if location and rotation:
            pos = {"location": list(location), "rotation": list(rotation)}
        else:
            pos = self._read_camera_position_robust()

        if not pos:
            carb.log_warn("[CustomMessageManager] registerShortcut: could not determine position")
            return

        key = name.lower().replace(" ", "_")
        self._shortcuts[key] = {
            "label": name,
            "description": description,
            "position": pos["location"],
            "rotation": pos["rotation"],
        }
        self._save_shortcuts()
        carb.log_info(f"[CustomMessageManager] Registered shortcut '{key}' at {pos['location']}")

        # Capture viewport thumbnail asynchronously
        asyncio.ensure_future(self._capture_shortcut_thumbnail(key))

        self._dispatch_shortcuts({"saved": True, "name": name})

    async def _capture_shortcut_thumbnail(self, key: str) -> None:
        """Capture the current viewport and save as a JPEG thumbnail for the shortcut."""
        import base64 as _b64
        try:
            frame_b64 = await self._viewport_capture.capture_frame_async(width=400, height=225)
            if not frame_b64:
                carb.log_warn(f"[CustomMessageManager] Shortcut thumbnail capture returned no data for '{key}'")
                return

            # Compress to small JPEG
            thumbnail_b64, _ = self._compress_planogram_frame(frame_b64)

            # Save to disk
            raw_bytes = _b64.b64decode(thumbnail_b64)
            thumb_path = os.path.join(self._shortcuts_thumbnails_dir, f"{key}.jpg")
            with open(thumb_path, "wb") as f:
                f.write(raw_bytes)
            carb.log_info(f"[CustomMessageManager] Saved shortcut thumbnail: {thumb_path} ({len(raw_bytes)} bytes)")

            # Push to frontend immediately
            get_eventdispatcher().dispatch_event(
                "shortcutThumbnail",
                payload={"key": key, "thumbnail": thumbnail_b64},
            )
        except Exception as e:
            carb.log_error(f"[CustomMessageManager] Shortcut thumbnail capture failed for '{key}': {e}")

    def _on_delete_shortcut(self, event: carb.events.IEvent) -> None:
        key = (event.payload or {}).get("key", "").strip()
        if key in self._shortcuts:
            del self._shortcuts[key]
            self._save_shortcuts()
            # Remove thumbnail file
            thumb_path = os.path.join(self._shortcuts_thumbnails_dir, f"{key}.jpg")
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
            carb.log_info(f"[CustomMessageManager] Deleted shortcut '{key}'")
        else:
            carb.log_warn(f"[CustomMessageManager] deleteShortcut: key '{key}' not found")
        self._dispatch_shortcuts({"deleted": True, "key": key})

    def _on_click_shortcut(self, event: carb.events.IEvent) -> None:
        """Navigate camera to a shortcut's saved position."""
        key = (event.payload or {}).get("key", "").strip()
        if key not in self._shortcuts:
            carb.log_warn(f"[CustomMessageManager] clickShortcut: key '{key}' not found")
            return
        shortcut = self._shortcuts[key]
        carb.log_info(f"[CustomMessageManager] clickShortcut '{key}' — navigating")
        asyncio.ensure_future(self._do_navigate_to_shortcut(key, shortcut))

    async def _do_navigate_to_shortcut(self, key: str, shortcut: Dict[str, Any]) -> None:
        """Navigate the camera to a shortcut and reply."""
        label = shortcut.get("label", key)
        position = shortcut.get("position", [0, 0, 0])
        rotation = shortcut.get("rotation", [0, 0, 0])

        nav = self._camera_navigation
        nav_key = f"__shortcut_{key}"
        nav.add_position(nav_key, tuple(position), tuple(rotation), label)
        await nav.navigate_to(nav_key)

        get_eventdispatcher().dispatch_event(
            "shortcutClickResponse",
            payload={"key": key, "label": label, "message": None, "loading": False},
        )

    def _on_clear_shortcuts(self, event: carb.events.IEvent) -> None:
        """Delete all shortcuts and their thumbnails."""
        # Remove thumbnail files
        for key in self._shortcuts:
            thumb_path = os.path.join(self._shortcuts_thumbnails_dir, f"{key}.jpg")
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
        self._shortcuts.clear()
        self._save_shortcuts()
        carb.log_info("[CustomMessageManager] Cleared all shortcuts")
        self._dispatch_shortcuts({"cleared": True})

    # ===== END NAVIGATION SHORTCUTS =====

    # =========================================================================
    # FIRE INCIDENT SIMULATION
    # =========================================================================

    def _on_fire_incident_request(self, event: carb.events.IEvent) -> None:
        """Handle fireIncidentRequest from the browser.

        Expected payload:
          action      : "trigger" | "extinguish" | "list"
          incident_id : str  (required for trigger/extinguish)
          position    : { x, y, z }  (optional for trigger — raw world position)
          screen_x    : float  (optional 0-1 — browser click, resolved via camera ray cast)
          screen_y    : float  (optional 0-1 — browser click, resolved via camera ray cast)
          severity    : str  (optional, default "high")
        """
        payload = event.payload or {}
        action      = payload.get("action", "").strip()
        incident_id = payload.get("incident_id", "").strip()
        severity    = payload.get("severity", "high")

        def _reply(result: str, message: str, extra: dict = None):
            response = {"result": result, "incident_id": incident_id, "message": message}
            if extra:
                response.update(extra)
            get_eventdispatcher().dispatch_event("fireIncidentResponse", payload=response)

        if action == "trigger":
            if not incident_id:
                _reply("error", "incident_id is required")
                return

            # Resolve position — priority: raw xyz → screen click ray cast
            position = None
            raw = payload.get("position", {})
            if raw:
                from pxr import Gf as _Gf
                position = _Gf.Vec3d(
                    float(raw.get("x", 0.0)),
                    float(raw.get("y", 0.0)),
                    float(raw.get("z", 0.0)),
                )
            elif "screen_x" in payload and "screen_y" in payload:
                position = self._usd_spawner._compute_world_position(
                    float(payload["screen_x"]),
                    float(payload["screen_y"]),
                )
                if position is None:
                    _reply("error", "Could not convert screen coordinates to world position")
                    return
            else:
                # Fallback: use a random ground position from the spawner
                position = self._usd_spawner._pick_random_ground_position()

            ok, msg = self._fire_manager.trigger_fire(
                incident_id, position, severity,
                fire_params=dict(payload.get("fire_params", {})) or None,
            )
            if ok:
                pos_list = self._fire_manager.list_active()[incident_id]["position"]
                _reply("ok", msg, {"position": {"x": pos_list[0], "y": pos_list[1], "z": pos_list[2]}})
            else:
                _reply("error", msg)

        elif action == "extinguish":
            if not incident_id:
                _reply("error", "incident_id is required")
                return
            ok, msg = self._fire_manager.extinguish_fire(incident_id)
            _reply("ok" if ok else "error", msg)

        elif action == "extinguish_all":
            ok, msg = self._fire_manager.extinguish_all()
            get_eventdispatcher().dispatch_event(
                "fireIncidentResponse",
                payload={"result": "ok" if ok else "error", "action": "extinguish_all", "message": msg},
            )

        elif action == "list":
            active = self._fire_manager.list_active()
            get_eventdispatcher().dispatch_event(
                "fireIncidentResponse",
                payload={"result": "ok", "action": "list", "incidents": active},
            )
        else:
            _reply("error", f"Unknown action '{action}'. Use trigger|extinguish|list")

    def _on_fire_adjust_request(self, event: carb.events.IEvent) -> None:
        """Adjust live flame parameters for an existing fire incident.

        Expected payload:
          incident_id  : str
          radius       : float  (optional)
          temperature  : float  (optional)
          fuel         : float  (optional)
          smoke        : float  (optional)
          velocity_up  : float  (optional)
        """
        payload = event.payload or {}
        incident_id = payload.get("incident_id", "").strip()

        def _reply(result: str, message: str):
            get_eventdispatcher().dispatch_event(
                "fireAdjustResponse",
                payload={"result": result, "incident_id": incident_id, "message": message},
            )

        if not incident_id:
            _reply("error", "incident_id is required")
            return

        def _opt(key: str):
            v = payload.get(key)
            return float(v) if v is not None else None

        ok, msg = self._fire_manager.adjust_fire(
            incident_id,
            radius=_opt("radius"),
            temperature=_opt("temperature"),
            fuel=_opt("fuel"),
            smoke=_opt("smoke"),
            velocity_up=_opt("velocity_up"),
        )
        _reply("ok" if ok else "error", msg)

    # ===== END FIRE INCIDENT SIMULATION =====

    # ===== ROBOT CONTROLLER HANDLERS =====

    def _init_robot(self):
        """Lazy-initialize the robot controller and wire up status push."""
        if not self._robot_controller:
            self._robot_controller = get_robot_controller()
        self._robot_controller.initialize()
        self._robot_controller.set_on_status(self._push_robot_status)

    def _push_robot_status(self, status: Dict[str, Any]):
        """Called by RobotController on every status change — push to frontend."""
        get_eventdispatcher().dispatch_event(
            "robotStatusUpdate",
            payload=status,
        )

    def _on_robot_command(self, event: carb.events.IEvent):
        """Handle direct input commands: forward, backward, turn_left, turn_right."""
        self._init_robot()
        payload = event.payload
        command = payload.get('command', '')
        distance = float(payload.get('distance', 50.0))
        degrees = float(payload.get('degrees', 90.0))

        rc = self._robot_controller
        if command == 'forward':
            result = rc.move_forward(distance)
        elif command == 'backward':
            result = rc.move_backward(distance)
        elif command == 'turn_left':
            result = rc.turn_left(degrees)
        elif command == 'turn_right':
            result = rc.turn_right(degrees)
        else:
            result = {'ok': False, 'error': f'Unknown command: {command}'}

        get_eventdispatcher().dispatch_event(
            "robotCommandResponse",
            payload=result,
        )

    def _on_robot_stop(self, event: carb.events.IEvent):
        self._init_robot()
        result = self._robot_controller.stop()
        get_eventdispatcher().dispatch_event("robotCommandResponse", payload=result)

    def _on_robot_reset(self, event: carb.events.IEvent):
        self._init_robot()
        result = self._robot_controller.reset()
        get_eventdispatcher().dispatch_event("robotCommandResponse", payload=result)

    def _on_robot_navigate_to_point(self, event: carb.events.IEvent):
        self._init_robot()
        payload = event.payload
        name = payload.get('name', '')
        result = self._robot_controller.navigate_to_point(name)
        get_eventdispatcher().dispatch_event("robotCommandResponse", payload=result)

    def _on_robot_navigate_route(self, event: carb.events.IEvent):
        self._init_robot()
        payload = event.payload
        route_name = payload.get('name', '')
        result = self._robot_controller.navigate_route(route_name)
        get_eventdispatcher().dispatch_event("robotCommandResponse", payload=result)

    def _on_robot_get_status(self, event: carb.events.IEvent):
        self._init_robot()
        status = self._robot_controller.get_status()
        get_eventdispatcher().dispatch_event("robotStatusResponse", payload=status)

    def _on_robot_build_nav_mesh(self, event: carb.events.IEvent):
        self._init_robot()
        info = self._robot_controller.build_nav_mesh()
        get_eventdispatcher().dispatch_event("robotStatusResponse", payload={"nav_mesh": info})

    def _on_robot_capture_frame(self, event: carb.events.IEvent):
        self._init_robot()
        payload = event.payload
        width = int(payload.get('width', 250))
        quality = int(payload.get('quality', 50))

        async def _do():
            result = await self._robot_controller.capture_frame(width=width, quality=quality)
            get_eventdispatcher().dispatch_event("robotCaptureResponse", payload=result)

        asyncio.ensure_future(_do())

    def _on_robot_get_nav_positions(self, event: carb.events.IEvent):
        self._init_robot()
        self._robot_controller.reload_from_disk()
        positions = self._robot_controller.get_nav_positions()
        get_eventdispatcher().dispatch_event("robotNavPositionsResponse", payload={"positions": positions})

    def _on_robot_get_routes(self, event: carb.events.IEvent):
        self._init_robot()
        self._robot_controller.reload_from_disk()
        routes = self._robot_controller.get_routes()
        get_eventdispatcher().dispatch_event("robotRoutesResponse", payload={"routes": routes})

    def _on_robot_get_grid_data(self, event: carb.events.IEvent):
        self._init_robot()
        grid_data = self._robot_controller.get_grid_data()
        if grid_data:
            get_eventdispatcher().dispatch_event("robotGridDataResponse", payload=grid_data)
        else:
            get_eventdispatcher().dispatch_event(
                "robotGridDataResponse",
                payload={"error": "Nav mesh not built. Click Build Nav Mesh first."}
            )

    # ===== END ROBOT CONTROLLER HANDLERS =====

    # =========================================================================
    # JOYSTICK CAMERA CONTROL
    # =========================================================================

    def _on_set_camera_speed(self, event: carb.events.IEvent) -> None:
        """Adjust viewport camera move speed from the browser joystick velocity slider."""
        payload = getattr(event, "payload", {}) or {}
        speed_level = int(payload.get("speed", 3))
        speed_map = {1: 20.0, 2: 50.0, 3: 100.0, 4: 200.0, 5: 400.0}
        speed_val = speed_map.get(speed_level, 100.0)
        try:
            import carb.settings as _cs
            s = _cs.get_settings()
            s.set("/persistent/app/viewport/manipulator/camera/flyAcceleration", speed_val)
            s.set("/persistent/app/viewport/manipulator/camera/flySpeed", speed_val)
            carb.log_info(f"[CustomMessageManager] Camera speed set to level {speed_level} ({speed_val})")
        except Exception as exc:
            carb.log_warn(f"[CustomMessageManager] setCameraSpeed failed: {exc}")

    def _on_joystick_move(self, event: carb.events.IEvent) -> None:
        """Move camera in the horizontal floor plane from browser joystick.

        dy < 0 = forward; dx > 0 = strafe right.
        Handles both Y-up (floor=XZ) and Z-up (floor=XY) scenes automatically.
        """
        payload = getattr(event, "payload", {}) or {}
        dx = float(payload.get("dx", 0.0))
        dy = float(payload.get("dy", 0.0))
        velocity = int(payload.get("velocity", 3))
        if abs(dx) < 0.05 and abs(dy) < 0.05:
            return

        speed_map = {1: 2.0, 2: 5.0, 3: 10.0, 4: 20.0, 5: 35.0}
        speed = speed_map.get(velocity, 10.0)

        try:
            import omni.usd
            from pxr import UsdGeom, Gf, Usd

            stage = omni.usd.get_context().get_stage()
            if not stage:
                return

            up_axis = UsdGeom.GetStageUpAxis(stage)
            z_up = (up_axis == UsdGeom.Tokens.z)

            result = self._camera_navigation._get_camera_and_ops()
            if result is None:
                return
            xformable, translate_op, _ = result
            if translate_op is None:
                return

            world_xform = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            look_w  = world_xform.TransformDir(Gf.Vec3d(0, 0, -1))
            right_w = world_xform.TransformDir(Gf.Vec3d(1, 0, 0))

            if z_up:
                fwd   = Gf.Vec3d(look_w[0],  look_w[1],  0.0)
                right = Gf.Vec3d(right_w[0], right_w[1], 0.0)
            else:
                fwd   = Gf.Vec3d(look_w[0],  0.0, look_w[2])
                right = Gf.Vec3d(right_w[0], 0.0, right_w[2])

            if fwd.GetLength() < 1e-6 or right.GetLength() < 1e-6:
                return
            fwd   = fwd.GetNormalized()
            right = right.GetNormalized()

            cur   = translate_op.Get()
            delta = fwd * (-dy * speed) + right * (dx * speed)

            if z_up:
                translate_op.Set(Gf.Vec3d(cur[0] + delta[0], cur[1] + delta[1], cur[2]))
            else:
                translate_op.Set(Gf.Vec3d(cur[0] + delta[0], cur[1], cur[2] + delta[2]))

        except Exception as exc:
            carb.log_warn(f"[CustomMessageManager] joystickMove error: {exc}")

    def _on_joystick_look(self, event: carb.events.IEvent) -> None:
        """Rotate camera from browser look-joystick (touch & drag event on screen with tablet).

        dx > 0 = look right (yaw);  dy > 0 = look down (pitch).
        Handles both Y-up and Z-up scenes automatically.
        """
        payload = getattr(event, "payload", {}) or {}
        dx = float(payload.get("dx", 0.0))
        dy = float(payload.get("dy", 0.0))
        sensitivity = int(payload.get("sensitivity", 3))
        if abs(dx) < 0.05 and abs(dy) < 0.05:
            return

        speed_map = {1: 0.4, 2: 0.8, 3: 1.5, 4: 2.5, 5: 4.0}
        deg_per_tick = speed_map.get(sensitivity, 1.5)

        try:
            import omni.usd
            from pxr import UsdGeom, Gf

            result = self._camera_navigation._get_camera_and_ops()
            if result is None:
                return
            _, _, rotate_op = result
            if rotate_op is None:
                return

            stage = omni.usd.get_context().get_stage()
            up_axis = UsdGeom.GetStageUpAxis(stage) if stage else UsdGeom.Tokens.z
            z_up = (up_axis == UsdGeom.Tokens.z)

            cur = rotate_op.Get()
            rx, ry, rz = float(cur[0]), float(cur[1]), float(cur[2])

            d_pitch = -dy * deg_per_tick   # up on stick = look up = negative pitch
            d_yaw   = -dx * deg_per_tick   # right on stick = look right

            rx += d_pitch
            # Clamp pitch to avoid flipping
            rx = max(0.0, min(150.0, rx))

            if z_up:
                rz += d_yaw
            else:
                ry += d_yaw

            rotate_op.Set(Gf.Vec3f(rx, ry, rz))

        except Exception as exc:
            carb.log_warn(f"[CustomMessageManager] joystickLook error: {exc}")

    # ===== END JOYSTICK CAMERA CONTROL =====

    def on_shutdown(self):
        """Clean up when the manager is shut down"""
        carb.log_info("[CustomMessageManager] Shutting down...")

        # Cancel pending requests
        for request_id in list(self._pending_requests.keys()):
            self._pending_requests[request_id]['cancelled'] = True
        self._pending_requests.clear()

        # Clear camera position tracking
        self._last_camera_positions.clear()

        # Shut down USD spawner
        if self._usd_spawner:
            self._usd_spawner.on_shutdown()
            self._usd_spawner = None

        # Extinguish any active fire incidents and remove Flow prims
        if self._fire_manager:
            self._fire_manager.on_shutdown()
            self._fire_manager = None

        # Shut down robot controller
        if self._robot_controller:
            self._robot_controller.shutdown()
            self._robot_controller = None

        # Clean up subscriptions
        for sub in self._subscriptions:
            sub.unsubscribe()
        self._subscriptions.clear()


# Module-level reference set by Extension.on_startup() for API server access
_manager_instance: Optional[CustomMessageManager] = None