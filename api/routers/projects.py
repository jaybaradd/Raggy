from fastapi import APIRouter
from api.schemas import CreateProjectRequest, ProjectListResponse, ProjectResponse
from db.session_store import session_store

router = APIRouter(prefix="/projects", tags=["projects"])

@router.get("", response_model=ProjectListResponse)
def list_projects() -> ProjectListResponse:
    return ProjectListResponse(projects=[ProjectResponse(project_id=p["project_id"], name=p["name"], created_at=p["created_at"]) for p in session_store.list_projects()])

@router.post("", response_model=ProjectResponse, status_code=201)
def create_project(body: CreateProjectRequest) -> ProjectResponse:
    p = session_store.create_project(body.name)
    return ProjectResponse(project_id=p["project_id"], name=p["name"], created_at=p["created_at"])
