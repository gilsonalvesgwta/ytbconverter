# YTB Conversor

Sistema web para baixar **trechos de áudio de vídeos do YouTube**.
Endereço: **https://ytbconversor.wtatech.com.br**

- Cole o link e clique em **Baixar**: o padrão já baixa de **00:00 a 02:00 em WAV**, na pasta **Downloads**.
- Em **Configurações** você muda o início e o fim, o formato (WAV, MP3, M4A, FLAC, OGG, OPUS), a qualidade, a taxa de amostragem, mono ou estéreo, o nome do arquivo e a pasta de destino.
- A opção "Manter estas configurações" guarda suas escolhas no navegador. Desmarcada, o sistema sempre volta ao padrão.
- A pasta de destino personalizada funciona no **Chrome ou Edge no computador** (o navegador pede permissão uma vez). Nos outros navegadores, o arquivo vai para a pasta de downloads do navegador.

O sistema é **independente** dos outros da VPS (n8n, CRM etc.): não publica nenhuma porta, só recebe acesso pelo Traefik no domínio próprio, e usa nomes exclusivos (`ytbconversor`) de stack, rota e volume.

---

## Instalação na VPS (passo a passo pelo Portainer)

O DNS `ytbconversor.wtatech.com.br` já aponta para a VPS. No Cloudflare, use a mesma opção de proxy (nuvem laranja ou cinza) dos outros endereços que já funcionam.

1. Abra o **Portainer** → **Stacks** → **+ Add stack**.
2. Em **Name**, digite: `ytbconversor`
3. Em **Build method**, escolha **Repository**:
   - **Repository URL**: `https://github.com/gilsonalvesgwta/ytbconverter`
   - **Repository reference**: `refs/heads/main`
   - **Compose path**: `deploy/stack.yml`
   - Deixe **GitOps updates** desligado (atualiza só quando você mandar).
4. Em **Environment variables**, clique em **+ Add an environment variable** e crie:

   | Nome | Valor | Para que serve |
   |---|---|---|
   | `SENHA_ACESSO` | uma senha sua | Protege o sistema (sem ela, qualquer pessoa com o link usa sua VPS). Também libera o envio de cookies. |

   Opcionais (já vêm com o valor certo; só crie se quiser mudar):

   | Nome | Padrão |
   |---|---|
   | `TRECHO_MAXIMO_SEGUNDOS` | `3600` (trecho de até 1 hora) |
   | `DOWNLOADS_SIMULTANEOS` | `3` |
   | `REDE_TRAEFIK` | `Wtatechor3Net` |
   | `RESOLVEDOR_TLS` | `letsencryptresolver` |
   | `ENTRADA_TRAEFIK` | `websecure` |

5. Clique em **Deploy the stack**.
6. Aguarde cerca de 1 minuto. Em **Services**, o serviço `ytbconversor_app` deve ficar em **1/1**.
7. Abra **https://ytbconversor.wtatech.com.br**, digite a senha, cole um link e clique em **Baixar**.

> Alternativa sem repositório: em **Build method** escolha **Web editor**, cole o conteúdo de `deploy/stack.yml` e crie as mesmas variáveis.

### Atualizar para uma versão nova

Portainer → **Stacks** → `ytbconversor` → **Pull and redeploy** (ou **Update the stack** com **Re-pull image** ligado).
O yt-dlp, que é a peça que conversa com o YouTube, também é atualizado sozinho toda vez que o serviço reinicia. Se um dia o YouTube mudar e os downloads pararem, basta reiniciar o serviço.

### Remover

Portainer → **Stacks** → `ytbconversor` → **Delete this stack**. Nada de outros sistemas é afetado. O volume `ytbconversor_dados` (que só guarda os cookies) pode ser apagado em **Volumes**.

---

## Se aparecer "verificação de robô" (cookies)

O YouTube às vezes bloqueia servidores. A solução é enviar os cookies de uma conta Google (de preferência uma conta secundária):

1. No Chrome do computador, instale a extensão **"Get cookies.txt LOCALLY"**.
2. Entre no **youtube.com** logado na conta.
3. Clique na extensão → **Export** (formato Netscape). Vai baixar um `cookies.txt`.
4. No YTB Conversor, abra **Configurações** → **Cookies do YouTube** → escolha o arquivo.
5. Pronto: a etiqueta muda para **configurado**. Tente o download de novo.

Os cookies ficam só no volume do serviço na VPS. Para tirar, clique em **Remover**.

---

## Usar pelo n8n (opcional)

Nó **HTTP Request**:

- **Method**: `GET`
- **URL**: `https://ytbconversor.wtatech.com.br/api/download`
- **Query parameters**: `url` (link do vídeo), `start` (ex. `00:00`), `end` (ex. `02:00`), `format` (`wav`, `mp3`, `m4a`, `flac`, `ogg`, `opus`), opcional `bitrate` (64 a 320), `samplerate`, `channels`
- **Header**: `X-Access-Key` = sua `SENHA_ACESSO`
- **Response format**: `File`

Sem `start`, `end` e `format`, vale o padrão (00:00 a 02:00, WAV).

---

## Para quem mantém o código

- `app/server.py`: servidor (só biblioteca padrão do Python + yt-dlp + ffmpeg). Corta direto do link do áudio com ffmpeg; se falhar, usa o próprio yt-dlp como plano B.
- `app/static/index.html`: interface.
- `tests/test_e2e.py`: 17 testes de ponta a ponta sem internet (`python3 tests/test_e2e.py`, precisa de ffmpeg).
- A cada envio para `main`, o GitHub Actions roda os testes, testa a imagem e publica `ghcr.io/gilsonalvesgwta/ytbconverter:latest` para **amd64 e arm64** (a VPS é ARM).
- Só aceita links do YouTube (impede que o sistema seja usado para acessar serviços internos da VPS).
- Atenção ao proxy do Cloudflare: ele corta respostas que levam mais de 100 segundos. Trechos curtos ficam prontos em poucos segundos; trechos muito longos em WAV podem passar disso.
