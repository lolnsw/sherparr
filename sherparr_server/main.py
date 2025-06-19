from fastapi import FastAPI, HTTPException
from datetime import datetime
from typing import Dict
import asyncio
from contextlib import asynccontextmanager

from shared.models import ClientStatus, PowerRequest, PowerStatus, PowerAction, SyncJobReport
from .tapo_controller import TapoController


clients_status: Dict[str, ClientStatus] = {}
tapo_controller: TapoController = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tapo_controller
    tapo_controller = TapoController()
    await tapo_controller.initialize()
    yield
    if tapo_controller:
        await tapo_controller.close()


app = FastAPI(
    title="Sherparr Server",
    description="Server for managing sync status and Tapo P110 smart plug control",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def root():
    return {"message": "Sherparr Server is running"}


@app.post("/client/status")
async def update_client_status(status: ClientStatus):
    clients_status[status.client_id] = status
    return {"message": "Status updated successfully"}


@app.get("/client/status/{client_id}")
async def get_client_status(client_id: str):
    if client_id not in clients_status:
        raise HTTPException(status_code=404, detail="Client not found")
    return clients_status[client_id]


@app.get("/clients/status")
async def get_all_clients_status():
    return clients_status


@app.get("/client/report/{client_id}")
async def get_client_sync_report(client_id: str):
    if client_id not in clients_status:
        raise HTTPException(status_code=404, detail="Client not found")
    
    client_status = clients_status[client_id]
    if not client_status.sync_report:
        raise HTTPException(status_code=404, detail="No sync report available for this client")
    
    return client_status.sync_report


@app.post("/power/control")
async def control_power(request: PowerRequest):
    try:
        if request.action == PowerAction.ON:
            await tapo_controller.turn_on()
        else:
            await tapo_controller.turn_off()
        return {"message": f"Power {request.action} successful"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/power/status")
async def get_power_status():
    try:
        status = await tapo_controller.get_status()
        return status
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now(),
        "active_clients": len(clients_status)
    }