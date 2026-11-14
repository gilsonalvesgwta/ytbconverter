#!/bin/sh
# O YouTube muda com frequência; o yt-dlp é atualizado ao iniciar o serviço
# (desligue com YTDLP_AUTO_UPDATE=false). Se falhar, segue com a versão da imagem.
if [ "${YTDLP_AUTO_UPDATE:-true}" = "true" ]; then
  echo "Atualizando o yt-dlp..."
  timeout 120 pip install --no-cache-dir -q -U "yt-dlp[default]" \
    && echo "yt-dlp $(python -c 'import yt_dlp;print(yt_dlp.version.__version__)')" \
    || echo "Não foi possível atualizar o yt-dlp agora; usando a versão da imagem."
fi
exec python /app/server.py
