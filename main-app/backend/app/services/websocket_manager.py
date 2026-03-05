"""
WebSocket manager for POD communication.
"""

import asyncio
from datetime import datetime
from typing import Any
from uuid import uuid4

import structlog
import websockets
from websockets.server import serve, WebSocketServerProtocol

from app.core.config import settings

logger = structlog.get_logger()


class WebSocketManager:
    """Manages WebSocket connections to POD agents."""
    
    def __init__(self):
        self._connections: dict[str, WebSocketServerProtocol] = {}
        self._pending_calls: dict[str, asyncio.Future] = {}
        self._server: Any = None
        self._heartbeat_task: asyncio.Task | None = None
    
    async def start_server(self):
        """Start the WebSocket server."""
        self._server = await serve(
            self._handle_connection,
            settings.HOST,
            settings.WEBSOCKET_PORT
        )
        logger.info("WebSocket server started", port=settings.WEBSOCKET_PORT)
        
        # Start heartbeat monitoring
        self._heartbeat_task = asyncio.create_task(self._monitor_heartbeats())
    
    async def shutdown(self):
        """Shutdown the WebSocket server."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        
        # Close all connections
        for pod_id, ws in self._connections.items():
            await ws.close()
        
        self._connections.clear()
        
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        
        logger.info("WebSocket server shutdown complete")
    
    async def _handle_connection(self, websocket: WebSocketServerProtocol):
        """Handle incoming WebSocket connection from POD."""
        pod_id = None
        
        try:
            # Wait for registration message
            message = await asyncio.wait_for(websocket.recv(), timeout=30)
            data = self._parse_message(message)
            
            if data.get("event") != "register":
                logger.warning("Expected registration message")
                await websocket.close()
                return
            
            pod_id = data.get("data", {}).get("pod_id")
            pod_name = data.get("data", {}).get("pod_name", "")
            
            if not pod_id:
                logger.warning("No pod_id in registration")
                await websocket.close()
                return
            
            # Reject if pod_name is empty
            if not pod_name:
                logger.warning("POD has no name, rejecting", pod_id=pod_id)
                await websocket.send(self._encode_message({
                    "event": "registration_rejected",
                    "data": {
                        "reason": "Pod name not configured. Re-generate the agent from the ReTrace UI.",
                        "pod_id": pod_id,
                    }
                }))
                await websocket.close()
                return
            
            # Check for duplicate pod_name in database
            from app.db.database import async_session_maker
            from app.models.pod import Pod
            from sqlalchemy import select
            
            async with async_session_maker() as session:
                existing = await session.execute(
                    select(Pod).where(
                        Pod.pod_name == pod_name,
                        Pod.pod_id != pod_id,
                    )
                )
                duplicate = existing.scalar_one_or_none()
            
            if duplicate:
                logger.warning("Duplicate pod_name rejected", pod_name=pod_name, pod_id=pod_id)
                await websocket.send(self._encode_message({
                    "event": "registration_rejected",
                    "data": {
                        "reason": f"Pod name '{pod_name}' is already taken. Run: ./retrace-agent --set-name <new-name>",
                        "pod_id": pod_id,
                    }
                }))
                await websocket.close()
                return
            
            # Store connection
            self._connections[pod_id] = websocket
            logger.info("POD connected", pod_id=pod_id, pod_name=pod_name)
            
            # Update POD status and name in database
            await self._update_pod_status(pod_id, "online", pod_name=pod_name)
            
            # Send acknowledgment
            await websocket.send(self._encode_message({
                "event": "registered",
                "data": {"status": "ok", "pod_id": pod_id, "pod_name": pod_name}
            }))
            
            # Handle messages
            async for message in websocket:
                await self._handle_message(pod_id, message)
                
        except asyncio.TimeoutError:
            logger.warning("Connection timeout during registration")
        except websockets.exceptions.ConnectionClosed:
            logger.info("POD disconnected", pod_id=pod_id)
        except Exception as e:
            import traceback
            logger.error("Error handling POD connection", error=str(e), traceback=traceback.format_exc())
        finally:
            if pod_id:
                self._connections.pop(pod_id, None)
                try:
                    await self._update_pod_status(pod_id, "offline")
                except Exception as e2:
                    logger.error("Failed to update pod status to offline", pod_id=pod_id, error=str(e2))
    
    async def _handle_message(self, pod_id: str, message: str):
        """Handle incoming message from POD."""
        data = self._parse_message(message)
        event = data.get("event")
        
        if event == "heartbeat":
            await self._handle_heartbeat(pod_id, data.get("data", {}))
        
        elif event == "rpc_response":
            await self._handle_rpc_response(data.get("data", {}))
        
        elif event == "training_progress":
            await self._handle_training_progress(pod_id, data.get("data", {}))
        
        elif event == "file_changed":
            await self._handle_file_change(pod_id, data.get("data", {}))
        
        else:
            logger.warning("Unknown event type", event=event, pod_id=pod_id)
    
    async def _handle_heartbeat(self, pod_id: str, data: dict):
        """Handle heartbeat from POD."""
        # Update last heartbeat in database
        from app.db.database import async_session_maker
        from app.models.pod import Pod
        from sqlalchemy import update
        
        async with async_session_maker() as session:
            await session.execute(
                update(Pod)
                .where(Pod.pod_id == pod_id)
                .values(
                    last_heartbeat=datetime.utcnow(),
                    status="online",
                    metadata_json=data.get("metrics", {})
                )
            )
            await session.commit()
    
    async def _handle_rpc_response(self, data: dict):
        """Handle RPC response from POD."""
        call_id = data.get("call_id")
        if call_id and call_id in self._pending_calls:
            future = self._pending_calls.pop(call_id)
            if data.get("error"):
                future.set_exception(Exception(data["error"]))
            else:
                future.set_result(data.get("result"))
    
    async def _handle_training_progress(self, pod_id: str, data: dict):
        """Handle training progress update from POD."""
        from app.db.database import async_session_maker
        from app.models.training import TrainingJob
        from sqlalchemy import update
        
        job_id = data.get("job_id")
        if not job_id:
            return
        
        async with async_session_maker() as session:
            await session.execute(
                update(TrainingJob)
                .where(TrainingJob.job_id == job_id)
                .values(progress_data=data)
            )
            await session.commit()
        
        logger.debug(
            "Training progress update",
            pod_id=pod_id,
            job_id=job_id,
            progress=data.get("progress"),
            total=data.get("total")
        )
    
    async def _handle_file_change(self, pod_id: str, data: dict):
        """Handle file change notification from POD."""
        logger.info(
            "File change detected",
            pod_id=pod_id,
            file_path=data.get("file_path"),
            change_type=data.get("change_type")
        )
        # TODO: Trigger incremental retraining if auto-sync is enabled
    
    async def _update_pod_status(self, pod_id: str, status: str, pod_name: str | None = None):
        """Update POD status in database."""
        from app.db.database import async_session_maker
        from app.models.pod import Pod
        from sqlalchemy import update
        
        values: dict = {
            "status": status,
            "last_heartbeat": datetime.utcnow() if status == "online" else None,
        }
        if pod_name:
            values["pod_name"] = pod_name
        
        try:
            async with async_session_maker() as session:
                await session.execute(
                    update(Pod)
                    .where(Pod.pod_id == pod_id)
                    .values(**values)
                )
                await session.commit()
            logger.info("Pod status updated", pod_id=pod_id, status=status)
        except Exception as e:
            logger.error("Failed to update pod status", pod_id=pod_id, status=status, error=str(e))
    
    async def _monitor_heartbeats(self):
        """Monitor POD heartbeats and mark stale connections as offline."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                
                from app.db.database import async_session_maker
                from app.models.pod import Pod
                from sqlalchemy import select, update
                
                async with async_session_maker() as session:
                    # Find PODs that haven't sent heartbeat in 90 seconds
                    cutoff = datetime.utcnow()
                    result = await session.execute(
                        select(Pod).where(
                            Pod.status == "online",
                            Pod.last_heartbeat < cutoff
                        )
                    )
                    stale_pods = result.scalars().all()
                    
                    for pod in stale_pods:
                        if pod.last_heartbeat:
                            time_since = (cutoff - pod.last_heartbeat).total_seconds()
                            if time_since > 90:
                                await session.execute(
                                    update(Pod)
                                    .where(Pod.pod_id == pod.pod_id)
                                    .values(status="offline")
                                )
                                logger.warning(
                                    "POD marked offline due to heartbeat timeout",
                                    pod_id=pod.pod_id
                                )
                    
                    await session.commit()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in heartbeat monitor", error=str(e))
    
    def is_pod_connected(self, pod_id: str) -> bool:
        """Check if POD is connected."""
        return pod_id in self._connections
    
    async def call_pod_method(
        self,
        pod_id: str,
        method: str,
        params: dict,
        timeout: float = 30.0
    ) -> dict:
        """Call an RPC method on a POD."""
        if pod_id not in self._connections:
            raise Exception(f"POD {pod_id} not connected")
        
        call_id = str(uuid4())
        
        # Create future for response
        future: asyncio.Future = asyncio.Future()
        self._pending_calls[call_id] = future
        
        try:
            # Send RPC request
            await self._connections[pod_id].send(self._encode_message({
                "event": "rpc_request",
                "data": {
                    "call_id": call_id,
                    "method": method,
                    "params": params
                }
            }))
            
            # Wait for response
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
            
        except asyncio.TimeoutError:
            self._pending_calls.pop(call_id, None)
            raise TimeoutError(f"POD {pod_id} did not respond to {method}")
    
    async def disconnect_pod(self, pod_id: str):
        """Disconnect a POD."""
        if pod_id in self._connections:
            await self._connections[pod_id].close()
            self._connections.pop(pod_id, None)
    
    def _parse_message(self, message: str | bytes) -> dict:
        """Parse incoming message."""
        import json
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        return json.loads(message)
    
    def _encode_message(self, data: dict) -> str:
        """Encode outgoing message."""
        import json
        return json.dumps(data)


# Global instance
websocket_manager = WebSocketManager()
