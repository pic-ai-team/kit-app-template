import asyncio
import json
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum

import carb


class AgentAction(Enum):
    NONE = "none"
    NAVIGATE_TO = "navigate_to"
    CAPTURE_FRAME = "capture_frame"
    GET_SCENE_INFO = "get_scene_info"
    HIGHLIGHT_OBJECT = "highlight_object"


@dataclass
class AgentResponse:
    message: str
    action: AgentAction = AgentAction.NONE
    action_params: Optional[Dict[str, Any]] = None
    requires_followup: bool = False
    session_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class ChatRequest:
    message: str
    session_id: str
    context: Optional[Dict[str, Any]] = None


class AgentClient:
    def __init__(self, base_url: str = "http://172.20.166.3:8000", timeout: float = 30.0):
        self._base_url = base_url.rstrip('/')
        self._timeout = timeout
        carb.log_info(f"[AgentClient] Initialized: {self._base_url}")

    async def send_chat_message(self, request: ChatRequest) -> AgentResponse:
        try:
            import aiohttp
        except ImportError:
            carb.log_warn("[AgentClient] aiohttp unavailable, using urllib fallback")
            return await self._send_chat_urllib(request)

        url = f"{self._base_url}/api/chat"
        payload = {
            "message": request.message,
            "session_id": request.session_id,
            "context": request.context or {},
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return self._parse_response(data)
                    else:
                        error_text = await response.text()
                        carb.log_error(f"[AgentClient] {response.status}: {error_text}")
                        return AgentResponse(message=f"Error: Server returned {response.status}")
        except asyncio.TimeoutError:
            carb.log_error("[AgentClient] Request timed out")
            return AgentResponse(message="Error: Request timed out. Please try again.")
        except Exception as e:
            carb.log_error(f"[AgentClient] Request failed: {e}")
            return AgentResponse(message=f"Error: {e}")

    async def _send_chat_urllib(self, request: ChatRequest) -> AgentResponse:
        import urllib.request
        import urllib.error

        url = f"{self._base_url}/api/chat"
        payload = {
            "message": request.message,
            "session_id": request.session_id,
            "context": request.context or {},
        }

        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                url, data=data,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=self._timeout)
            )
            response_data = json.loads(response.read().decode('utf-8'))
            return self._parse_response(response_data)
        except urllib.error.HTTPError as e:
            carb.log_error(f"[AgentClient] HTTP {e.code}")
            return AgentResponse(message=f"Error: Server returned {e.code}")
        except Exception as e:
            carb.log_error(f"[AgentClient] Request failed: {e}")
            return AgentResponse(message=f"Error: {e}")

    def _parse_response(self, data: Dict[str, Any]) -> AgentResponse:
        action_str = data.get('action', 'none')
        try:
            action = AgentAction(action_str)
        except ValueError:
            action = AgentAction.NONE

        return AgentResponse(
            message=data.get('message', ''),
            action=action,
            action_params=data.get('action_params'),
            requires_followup=data.get('requires_followup', False),
            session_id=data.get('session_id'),
            metadata=data.get('metadata'),
        )

    async def health_check(self) -> bool:
        try:
            import urllib.request
            url = f"{self._base_url}/health"
            req = urllib.request.Request(url, method='GET')
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=5.0)
            )
            return response.status == 200
        except Exception as e:
            carb.log_warn(f"[AgentClient] Health check failed: {e}")
            return False
