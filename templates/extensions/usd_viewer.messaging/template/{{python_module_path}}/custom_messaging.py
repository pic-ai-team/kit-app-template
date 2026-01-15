# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

import carb
import carb.events
from carb.eventdispatcher import get_eventdispatcher
import omni.kit.app
import omni.kit.livestream.messaging as messaging
from omni.timeline import get_timeline_interface


class CustomMessageManager:
    """Manages custom messages between web client and Kit application"""

    def __init__(self):
        """Initialize the custom message manager"""
        self._subscriptions = []
        self._timeline = get_timeline_interface()
        carb.log_info("[CustomMessageManager] Initializing...")

        # ===== REGISTER OUTGOING MESSAGES (Kit -> Web Client) =====
        outgoing_messages = [
            "customActionResult",       # Response to custom action requests
            "dataUpdateNotification",   # Notify client of data changes
            "parameterChanged",         # Confirm parameter changes
            "timelineStatusResponse",   # Timeline/simulation status response
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

    def on_shutdown(self):
        """Clean up when the manager is shut down"""
        carb.log_info("[CustomMessageManager] Shutting down...")

        # Clean up subscriptions
        for sub in self._subscriptions:
            sub.unsubscribe()
        self._subscriptions.clear()