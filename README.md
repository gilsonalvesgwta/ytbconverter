# YTB Conversor

Sistema web para baixar **trechos de áudio de vídeos do YouTube** e **transcrever vídeos e áudios por completo**.
Endereço: **https://ytbconversor.wtatech.com.br**

- Cole o link e clique em **Baixar**: o padrão já baixa **do início até 2 minutos (ou o vídeo inteiro, se for menor) em WAV**, na pasta **Downloads**.
- Em **Configurações** você muda o início e o fim, o formato (WAV, MP3, M4A, FLAC, OGG, OPUS), a qualidade, a taxa de amostragem, mono ou estéreo, o nome do arquivo e a pasta de destino.
- A opção "Manter estas configurações" guarda suas escolhas no navegador. Desmarcada, o sistema sempre volta ao padrão.
- A pasta de destino personalizada funciona no **Chrome ou Edge no computador** (o navegador pede permissão uma vez). Nos outros navegadores, o arquivo vai para a pasta de downloads do navegador.

## Transcrição (aba "Transcrever")

1. Clique na área pontilhada e escolha um vídeo ou áudio do computador (MP4, MOV, MKV, MP3, WAV, M4A, OGG… até 4 GB). Também dá para colar um link do YouTube.
2. Clique em **Transcrever**. A barra mostra o envio e o andamento.
3. No fim, clique em **Copiar transcrição**: copia só o texto, inteiro, com um clique. Também há **Baixar .txt**.

Se você recarregar a página no meio, ela retoma a transcrição em andamento.

**Rapidez da transcrição:**
- Em vídeos MP4, MOV e M4A, o próprio navegador separa só o áudio (sem converter, sem perda) e envia apenas ele: um vídeo de 2 GB com 1 hora sobe cerca de 60 MB.
- A transcrição começa enquanto o envio ainda está acontecendo, e o texto vai aparecendo na tela. Outros formatos (MKV, MP3, WAV…) sobem como estão, mas também são transcritos durante o envio.
- O modelo gratuito fica carregado na memória do servidor, pronto para começar na hora.
- Na OpenAI, até 4 partes de 5 minutos são transcritas ao mesmo tempo.
- O que mais acelera o modo gratuito é dar mais processadores ao serviço (variável `LIMITE_CPU`) ou usar um modelo menor.

O motor gratuito transcreve **o áudio inteiro**, inclusive músicas e falas com música de fundo. A opção "Pular trechos sem fala (modo rápido)" existe para palestras e reuniões com muitas pausas, mas pode pular canto. Deixe desmarcada para ter tudo.

**Espaço na VPS:** o arquivo enviado fica só temporariamente. Ele é apagado assim que o áudio é extraído (antes mesmo de terminar a transcrição), e a pasta de trabalho é apagada ao terminar. Envios abandonados (navegador fechado no meio) são apagados sozinhos depois de 1 hora, e qualquer sobra é limpa quando o serviço reinicia. O texto da transcrição fica só na memória (24 h) e no seu navegador. O que fica guardado de forma permanente é apenas o modelo gratuito baixado (cerca de 500 MB para o Small), para não precisar baixar de novo a cada uso.

**Motores (em "Configurações da transcrição", configurados uma única vez):**

| Motor | Custo | Observação |
|---|---|---|
| **Gratuito** (padrão) | R$ 0 | Whisper rodando na própria VPS. Na 1ª vez com cada modelo, o servidor baixa o modelo (alguns minutos) e guarda. Velocidade depende do processador da VPS. |
| **OpenAI** | ~US$ 0,003/min (≈ US$ 0,18 por hora de áudio) | Modelo `gpt-4o-mini-transcribe`, o mais barato da OpenAI. Mais rápido. Precisa da chave da API (platform.openai.com/api-keys) e de saldo na conta. |

Modelos gratuitos: Tiny (mais rápido) → Base → **Small (recomendado)** → Medium → Large v3 Turbo (melhor e mais lento).
A chave da OpenAI fica salva só no servidor (nunca aparece inteira na tela) e exige a variável `SENHA_ACESSO`.
A API do Claude não transcreve áudio, por isso a opção paga é a da OpenAI.

**Rapidez:** o sistema não baixa o vídeo. Ele pega só a faixa de áudio (sem imagem), pula direto para o início pedido e lê só os bytes do trecho, convertendo ao mesmo tempo. Num áudio de 2 horas, um trecho de 2 minutos fica pronto em menos de 1 segundo de processamento, esteja no começo, no meio ou no fim. Ao colar o link, o sistema já lê os dados do vídeo enquanto você confere a prévia, e o clique em **Baixar** começa direto no corte.

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
   | `LIMITE_CPU` | `2` (processadores para o serviço; mais = transcrição gratuita mais rápida) |
   | `LIMITE_MEMORIA` | `3072M` |
   | `UPLOAD_MAXIMO_MB` | `4096` (arquivo de até 4 GB para transcrever) |
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

Portainer → **Stacks** → `ytbconversor` → **Delete this stack**. Nada de outros sistemas é afetado. O volume `ytbconversor_dados` (guarda cookies, configurações da transcrição e modelos baixados) pode ser apagado em **Volumes**.

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

Transcrição pela API: `POST /api/transcribe/start` (`name`, `size`, opcional `language`) → `PUT /api/upload/{upload_id}?offset=N` (partes de até 50 MB; a transcrição já começa) → consultar `GET /api/jobs/{job.id}` até `status` = `done` (texto em `text`). Para link do YouTube: `POST /api/transcribe` com `url`. Sempre com o header `X-Access-Key`.

---

## Para quem mantém o código

- `app/server.py`: servidor (só biblioteca padrão do Python + yt-dlp + ffmpeg). Corta direto do link do áudio com ffmpeg; se falhar, usa o próprio yt-dlp como plano B.
- `app/transcribe.py`: transcrição (faster-whisper local ou OpenAI), envio em partes e fila de tarefas.
- `app/static/index.html`: interface.
- `tests/test_e2e.py`: 18 testes dos trechos de áudio; `tests/test_transcribe.py`: testes da transcrição (OpenAI simulada + Whisper com voz sintetizada); `tests/smoke_transcribe.py`: teste rápido contra um servidor rodando.
- A cada envio para `main`, o GitHub Actions roda os testes, testa a imagem e publica `ghcr.io/gilsonalvesgwta/ytbconverter:latest` para **amd64 e arm64** (a VPS é ARM).
- Só aceita links do YouTube (impede que o sistema seja usado para acessar serviços internos da VPS).
- O proxy do Cloudflare limita cada envio a 100 MB e cada resposta a 100 segundos: por isso arquivos sobem em partes de 50 MB e a transcrição roda em segundo plano.
- Atenção ao proxy do Cloudflare: ele corta respostas que levam mais de 100 segundos. Trechos curtos ficam prontos em poucos segundos; trechos muito longos em WAV podem passar disso.
