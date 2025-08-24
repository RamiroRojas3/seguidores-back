from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, PleaseWaitFewMinutes, ChallengeRequired
import os
import json
from typing import Optional, List, Dict
import uvicorn
from datetime import datetime
import logging

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Instagram API Backend", version="1.0.0")
security = HTTPBearer()

# CORS para permitir requests desde Android
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción, especifica los dominios permitidos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Modelos Pydantic para requests/responses
class LoginRequest(BaseModel):
    username: str
    password: str


class PostContentRequest(BaseModel):
    caption: str
    image_path: Optional[str] = None


class GetUserInfoRequest(BaseModel):
    username: str


class SearchRequest(BaseModel):
    query: str
    limit: int = 10


class LoginResponse(BaseModel):
    success: bool
    message: str
    session_token: Optional[str] = None


class UserInfoResponse(BaseModel):
    user_id: str
    username: str
    full_name: str
    followers_count: int
    following_count: int
    media_count: int
    biography: str
    profile_pic_url: str


# Almacenamiento en memoria de sesiones (en producción usa Redis/BD)
active_sessions: Dict[str, Client] = {}
SESSION_FILE_DIR = "sessions"

# Crear directorio de sesiones si no existe
os.makedirs(SESSION_FILE_DIR, exist_ok=True)


def get_session_file_path(username: str) -> str:
    """Obtiene la ruta del archivo de sesión"""
    return os.path.join(SESSION_FILE_DIR, f"{username}_session.json")


def save_session(client: Client, username: str):
    """Guarda la sesión en archivo"""
    try:
        session_data = client.get_settings()
        with open(get_session_file_path(username), 'w') as f:
            json.dump(session_data, f)
        logger.info(f"Sesión guardada para {username}")
    except Exception as e:
        logger.error(f"Error al guardar sesión: {e}")


def load_session(client: Client, username: str) -> bool:
    """Carga la sesión desde archivo"""
    try:
        session_file = get_session_file_path(username)
        if os.path.exists(session_file):
            with open(session_file, 'r') as f:
                session_data = json.load(f)
            client.set_settings(session_data)
            client.login(username)
            logger.info(f"Sesión cargada para {username}")
            return True
    except Exception as e:
        logger.error(f"Error al cargar sesión: {e}")
    return False


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Verifica el token de autorización"""
    token = credentials.credentials
    if token not in active_sessions:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado"
        )
    return token


@app.post("/api/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """Endpoint para login de Instagram"""
    try:
        client = Client()

        # Intentar cargar sesión existente
        if load_session(client, request.username):
            # Generar token de sesión
            session_token = f"{request.username}_{datetime.now().timestamp()}"
            active_sessions[session_token] = client

            return LoginResponse(
                success=True,
                message="Login exitoso (sesión reutilizada)",
                session_token=session_token
            )

        # Login nuevo
        client.login(request.username, request.password)

        # Guardar sesión
        save_session(client, request.username)

        # Generar token de sesión
        session_token = f"{request.username}_{datetime.now().timestamp()}"
        active_sessions[session_token] = client

        return LoginResponse(
            success=True,
            message="Login exitoso",
            session_token=session_token
        )

    except LoginRequired:
        raise HTTPException(status_code=400, detail="Credenciales incorrectas")
    except ChallengeRequired as e:
        raise HTTPException(status_code=400, detail="Challenge requerido. Verifica tu cuenta desde la app oficial.")
    except PleaseWaitFewMinutes:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera unos minutos.")
    except Exception as e:
        logger.error(f"Error en login: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


@app.post("/api/logout")
async def logout(token: str = Depends(verify_token)):
    """Endpoint para logout"""
    try:
        if token in active_sessions:
            del active_sessions[token]
        return {"success": True, "message": "Logout exitoso"}
    except Exception as e:
        logger.error(f"Error en logout: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


@app.get("/api/user-info", response_model=UserInfoResponse)
async def get_user_info(username: str, token: str = Depends(verify_token)):
    """Obtiene información de un usuario"""
    try:
        client = active_sessions[token]
        user = client.user_info_by_username(username)

        return UserInfoResponse(
            user_id=str(user.pk),
            username=user.username,
            full_name=user.full_name or "",
            followers_count=user.follower_count,
            following_count=user.following_count,
            media_count=user.media_count,
            biography=user.biography or "",
            profile_pic_url=user.profile_pic_url or ""
        )
    except Exception as e:
        logger.error(f"Error al obtener info de usuario: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/api/user-posts")
async def get_user_posts(username: str, limit: int = 12, token: str = Depends(verify_token)):
    """Obtiene los posts de un usuario"""
    try:
        client = active_sessions[token]
        user_id = client.user_id_from_username(username)
        posts = client.user_medias(user_id, amount=limit)

        posts_data = []
        for post in posts:
            posts_data.append({
                "id": str(post.pk),
                "caption": post.caption_text or "",
                "media_type": post.media_type,
                "thumbnail_url": post.thumbnail_url,
                "like_count": post.like_count,
                "comment_count": post.comment_count,
                "taken_at": post.taken_at.isoformat() if post.taken_at else None
            })

        return {"success": True, "posts": posts_data}
    except Exception as e:
        logger.error(f"Error al obtener posts: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.post("/api/upload-photo")
async def upload_photo(caption: str, image_path: str, token: str = Depends(verify_token)):
    """Sube una foto a Instagram"""
    try:
        client = active_sessions[token]

        if not os.path.exists(image_path):
            raise HTTPException(status_code=400, detail="Archivo de imagen no encontrado")

        media = client.photo_upload(image_path, caption)

        return {
            "success": True,
            "message": "Foto subida exitosamente",
            "media_id": str(media.pk)
        }
    except Exception as e:
        logger.error(f"Error al subir foto: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.post("/api/like-post")
async def like_post(media_id: str, token: str = Depends(verify_token)):
    """Da like a un post"""
    try:
        client = active_sessions[token]
        result = client.media_like(media_id)

        return {"success": result, "message": "Like enviado" if result else "Error al dar like"}
    except Exception as e:
        logger.error(f"Error al dar like: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.post("/api/comment-post")
async def comment_post(media_id: str, text: str, token: str = Depends(verify_token)):
    """Comenta en un post"""
    try:
        client = active_sessions[token]
        comment = client.media_comment(media_id, text)

        return {
            "success": True,
            "message": "Comentario enviado",
            "comment_id": str(comment.pk)
        }
    except Exception as e:
        logger.error(f"Error al comentar: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/api/search-users")
async def search_users(query: str, limit: int = 10, token: str = Depends(verify_token)):
    """Busca usuarios"""
    try:
        client = active_sessions[token]
        users = client.search_users(query, amount=limit)

        users_data = []
        for user in users:
            users_data.append({
                "user_id": str(user.pk),
                "username": user.username,
                "full_name": user.full_name or "",
                "profile_pic_url": user.profile_pic_url or "",
                "is_verified": user.is_verified,
                "follower_count": user.follower_count
            })

        return {"success": True, "users": users_data}
    except Exception as e:
        logger.error(f"Error en búsqueda: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/api/followers")
async def get_followers(username: str, limit: int = 50, token: str = Depends(verify_token)):
    """Obtiene los seguidores de un usuario"""
    try:
        client = active_sessions[token]
        user_id = client.user_id_from_username(username)
        followers = client.user_followers(user_id, amount=limit)

        followers_data = []
        for follower_id, follower in followers.items():
            followers_data.append({
                "user_id": str(follower.pk),
                "username": follower.username,
                "full_name": follower.full_name or "",
                "profile_pic_url": follower.profile_pic_url or ""
            })

        return {"success": True, "followers": followers_data}
    except Exception as e:
        logger.error(f"Error al obtener seguidores: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "active_sessions": len(active_sessions)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)