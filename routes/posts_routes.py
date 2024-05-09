import tempfile
from config.security import SSH_USERNAME_RES, SSH_PASSWORD_RES, SSH_HOST_RES, \
    get_current_user, get_user_id
import random
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from bson import ObjectId
from typing import List
import paramiko
from model.post_shemas import Post, PostInDB, PostShowed, NewPost
from config.db import get_database, post_collection, users_collection, interactions_collection, lyrics_collection
from config.security import get_current_user, get_user_id, get_username
from model.user_shemas import User
from routes.interactions_routes import count_likes, count_dislikes, count_saved
from fastapi import File, UploadFile, HTTPException
import os
import shutil
import zipfile
from datetime import datetime
import io

router = APIRouter()

# Definir la ruta donde se guardarán los archivos temporales
TEMP_DIRECTORY = "/var/www/html/beatnow/temp"


async def compress_and_upload_audio(audio_file: UploadFile, post_id: str, post_dir: str):
    try:
        # Comprimir el archivo de audio
        compressed_audio = await compress_audio(audio_file)

        # Crear un archivo zip en memoria
        zip_file = io.BytesIO()
        with zipfile.ZipFile(zip_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr(f"post_{post_id}.mp3", compressed_audio)

        # Subir el archivo zip al servidor remoto
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=SSH_HOST_RES, username=SSH_USERNAME_RES, password=SSH_PASSWORD_RES)

            with ssh.open_sftp().file(os.path.join(post_dir, f"post_{post_id}.zip"), "wb") as buffer:
                buffer.write(zip_file.getvalue())

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")


async def compress_audio(audio_file: UploadFile) -> bytes:
    try:
        # Leer el archivo de audio en memoria
        audio_content = await audio_file.read()

        # Comprimir el archivo de audio
        compressed_audio = io.BytesIO()
        with zipfile.ZipFile(compressed_audio, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr(audio_file.filename, audio_content)

        return compressed_audio.getvalue()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred while compressing audio: {str(e)}")


async def save_temporary_file(upload_file: UploadFile, post_id: str) -> str:
    try:
        # Crear un archivo temporal con un nombre fijo
        with tempfile.NamedTemporaryFile(prefix=f"beat_temporal_{post_id}", delete=False) as temp_file:
            # Copiar los datos del archivo de carga en el archivo temporal
            shutil.copyfileobj(upload_file.file, temp_file)
            temp_file_name = temp_file.name  # Obtener el nombre del archivo temporal
        # Devolver la ruta del archivo temporal creado
        return temp_file_name
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")



@router.post("/upload-post", response_model=PostInDB)
async def upload_post(
    file: UploadFile = File(...),
    audio_file: UploadFile = File(...),
    new_post: NewPost = Depends(),
    current_user: User = Depends(get_current_user),
):
    # Validar el tipo de archivo antes de continuar
    allowed_image_extensions = {".jpg", ".jpeg"}
    allowed_audio_extensions = {".wav", ".mp3", ".flac"}

    if not file.filename.lower().endswith(tuple(allowed_image_extensions)):
        raise HTTPException(status_code=415, detail="Only JPG/JPEG files are allowed for images.")

    if audio_file:
        if not audio_file.filename.lower().endswith(tuple(allowed_audio_extensions)):
            raise HTTPException(status_code=415, detail="Only MP3/WAV files are allowed for audio.")

    # Obtener ID del usuario
    user_id = await get_user_id(current_user.username)
    if user_id == "Usuario no encontrado":
        raise HTTPException(status_code=404, detail="User not found")

    # Crear el post en la base de datos
    result = None
    try:
        post = Post(
            user_id=str(ObjectId(user_id)),
            publication_date=datetime.now(),
            **new_post.dict()
        )
        result = await post_collection.insert_one(post.dict())
        if not result.inserted_id:
            raise HTTPException(status_code=500, detail="Failed to create publication")

        post_id = str(result.inserted_id)
        post_dir = f"/var/www/html/beatnow/{current_user.username}/posts/{post_id}/"

        # Configuración de SSH
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=SSH_HOST_RES, username=SSH_USERNAME_RES, password=SSH_PASSWORD_RES)

            # Verificar si el directorio del usuario existe, si no, crearlo
            if not ssh.exec_command(f"test -d {post_dir}")[1].read():
                # Crear directorios en el servidor remoto
                ssh.exec_command(f"sudo mkdir -p {post_dir}")
                ssh.exec_command(f"sudo chown -R $USER:$USER {post_dir}")

            # Guardar la nueva foto de perfil con un nombre único y formato png
            file_path = os.path.join(post_dir, "caratula.jpg")
            with ssh.open_sftp().file(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            # Guardar el archivo de audio con el nombre "beat.wav" o "beat.mp3"
            if audio_file:
                # Guardar el archivo de audio temporalmente
                audio_temp_path = await save_temporary_file(audio_file, post_id)

                # Subir el archivo temporal al servidor remoto
                await compress_and_upload_audio(audio_file, post_id, post_dir)

    except paramiko.SSHException as e:
        if result:
            # Eliminar el post de la base de datos si se ha creado antes de la excepción
            await post_collection.delete_one({"_id": result.inserted_id})
        raise HTTPException(status_code=500, detail=f"SSH error: {str(e)}")
    except Exception as e:
        if result:
            # Eliminar el post de la base de datos si se ha creado antes de la excepción
            await post_collection.delete_one({"_id": result.inserted_id})
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

    return PostInDB(_id=post_id, **post.dict())


@router.get("/random-publication", response_model=PostShowed)
async def get_random_publication(current_user: User = Depends(get_current_user), db=Depends(get_database)):
    # Fetch all post IDs
    post_ids = await post_collection.find({}, {"_id": 1}).to_list(length=None)

    if not post_ids:
        raise HTTPException(status_code=404, detail="No publications found")

    # Select a random ID from the list
    random_post_id = random.choice(post_ids)['_id']

    # Use the existing read_publication function to fetch and return the publication details
    return await read_publication(str(random_post_id), current_user, db)

@router.get("/{post_id}", response_model=PostShowed)
async def read_publication(post_id: str, current_user: User = Depends(get_current_user), db=Depends(get_database)):
    post_dict = await post_collection.find_one({"_id": ObjectId(post_id)})
    if post_dict:
        postindb = Post(**post_dict)
        creator_name = await get_username(post_dict["user_id"])  # Use post_dict instead of post_id
        post = PostShowed(_id=str(ObjectId(post_id)), **postindb.dict(), likes=await count_likes(post_id), dislikes=await count_dislikes(post_id),
                          saves=await count_saved(post_id), creator_username=creator_name,isLiked=await has_liked_post(post_id, current_user),
                          isSaved=await has_saved_post(post_id, current_user))
        return post
    else:
        raise HTTPException(status_code=404, detail="Publication not found")

@router.put("/{post_id}", response_model=PostInDB)
async def update_publication(post_id: str, publication: NewPost, current_user: User = Depends(get_current_user),
                             db=Depends(get_database)):
    existing_publication = await post_collection.find_one({"_id": ObjectId(post_id)})
    if existing_publication:
        if str(existing_publication["user_id"]) != str(await get_user_id(current_user.username)):
            raise HTTPException(status_code=403, detail="You are not authorized to update this publication")

        # Actualizar los datos existentes con los datos de la publicación
        existing_publication.update(publication.dict(exclude_unset=True))

        # Actualizar la publicación en la base de datos
        result = await post_collection.update_one({"_id": ObjectId(post_id)}, {"$set": existing_publication})

        # Convertir ObjectId a string para el retorno
        existing_publication["_id"] = str(existing_publication["_id"])
        return existing_publication
    else:
        raise HTTPException(status_code=404, detail="Publication not found")

#no elimina la publicacion de la base de datos ni servidor
# Eliminar publicación por ID
@router.delete("/{post_id}")
async def delete_publication(post_id: str, current_user: User = Depends(get_current_user), db=Depends(get_database)):
    existing_publication = await post_collection.find_one({"_id": ObjectId(post_id)})
    if existing_publication:
        if existing_publication["user_id"] != await get_user_id(current_user.username):
            raise HTTPException(status_code=403, detail="You are not authorized to delete this publication")
        await interactions_collection.delete_many({"post_id:": post_id})
        await lyrics_collection.update_many({"post_id:": post_id},{ "$set": { "post_id": "None" } })
        result = await post_collection.delete_one({"_id": ObjectId(post_id)})
        if result.deleted_count == 1:
            return {"message": "Publication deleted successfully"}
    raise HTTPException(status_code=404, detail="Publication not found")


# Listar todas las publicaciones
@router.get("/user/{username}", response_model=List[Post])
async def list_user_publications(username: str, current_user: User = Depends(get_current_user),
                                 db=Depends(get_database)):
    # Verificar si el usuario solicitado existe
    user_exists = await users_collection.find_one({"username": username})
    if not user_exists:
        raise HTTPException(status_code=404, detail="User not found")

    # Buscar todas las publicaciones del usuario
    user_id = await get_user_id(username)
    user_publications = await post_collection.find({"user_id": user_id}).to_list(length=None)

    # Comprobar si se encontraron publicaciones
    if user_publications:
        return user_publications
    else:
        raise HTTPException(status_code=404, detail="User has no publications")

async def has_liked_post(post_id: str, current_user: User):
    user_id = await get_user_id(current_user.username)
    like_exists = await interactions_collection.count_documents({"user_id": user_id, "post_id": post_id, "like_date": {"$exists": True}})
    return like_exists > 0

async def has_saved_post(post_id: str, current_user: User):
    user_id = await get_user_id(current_user.username)
    saved_exists = await interactions_collection.count_documents({"user_id": user_id, "post_id": post_id, "saved_date": {"$exists": True}})
    return saved_exists > 0

@router.post("/change_post_cover/{post_id}")
async def change_post_cover(post_id: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        # Obtener el post
        post = await lyrics_collection.find_one({"_id": post_id})
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        # Verificar si el usuario tiene permisos para editar el post
        if post["user_id"] != current_user.id:
            raise HTTPException(status_code=403, detail="Unauthorized to edit this post")

        # Guardar la nueva carátula con un nombre único y formato png
        file_path = os.path.join("/var/www/html/beatnow", current_user.username, "posts", post_id, "cover.png")
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

    return {"message": "Post cover updated successfully"}

@router.post("/delete_post_cover/{post_id}")
async def delete_post_cover(post_id: str, current_user: User = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        # Obtener el post
        post = await post_collection.find_one({"_id": post_id})
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        # Verificar si el usuario tiene permisos para editar el post
        if post["user_id"] != current_user.id:
            raise HTTPException(status_code=403, detail="Unauthorized to edit this post")

        # Eliminar la carátula del post
        post_dir = f"/var/www/html/beatnow/{current_user.username}/posts/{post_id}"
        os.remove(os.path.join(post_dir, "cover.png"))

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

    return {"message": "Post cover deleted successfully"}

async def count_user_posts(user_id: str, current_user: User = Depends(get_current_user), db=Depends(get_database)):
    # Verificar si el usuario tiene permisos para ver las publicaciones del usuario
    if user_id != await get_user_id(current_user.username):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # Contar las publicaciones del usuario
    count = await post_collection.count_documents({"user_id": user_id})
    return count

