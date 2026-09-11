# GitHub → Render

1. Extract this ZIP.
2. Upload the files to a private GitHub repository.
3. Create a Render Blueprint from the repository.
4. In the worker environment, set:
   BOT_TOKEN
   SEEDR_EMAIL
   SEEDR_PASSWORD
   PCLOUD_EMAIL
   PCLOUD_PASSWORD
5. Set PCLOUD_FOLDER_PATH, for example /Movies.
6. Leave DELETE_ALL_SEEDR_FILES_AFTER_UPLOAD=true if you really want the Seedr account cleaned after each successful upload.
7. Check the web service /health endpoint.
8. Open Telegram, send /start, then send a legally authorized magnet.

Never commit passwords or bot tokens to GitHub. Enter them only in Render's private environment variables.
