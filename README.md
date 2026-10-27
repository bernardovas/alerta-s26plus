# Alerta de preço — Galaxy S26+ (Windows)

Robô que verifica a cada 2 horas o preço do **Samsung Galaxy S26+** na Amazon, Magazine Luiza, Mercado Livre e Casas Bahia, e manda no **Telegram** um resumo com o menor preço de cada loja. Anúncios abaixo de **R$ 4.900** aparecem em destaque no topo da mensagem. Roda no seu PC pelo Agendador de Tarefas do Windows (as lojas bloqueiam os servidores do GitHub).

## Instalação

1. Dê dois cliques em **`instalar.bat`**.
2. Cole o token do bot (do @BotFather) e o seu ID do Telegram quando pedir.
3. No fim, chega um resumo de teste no Telegram.

O instalador baixa o Python se precisar, instala as bibliotecas numa pasta isolada (`.venv`), cria a tarefa **"Alerta S26 Plus"** e roda um teste.

## Arquivos úteis

- `testar.bat` — roda uma verificação agora e manda o resumo no Telegram.
- `log.txt` — o que aconteceu em cada verificação automática.
- `config.json` — preço máximo, cores excluídas, armazenamentos e lojas.
- `desinstalar.bat` — remove o agendamento.

## Regras aplicadas

Só gera alerta se o anúncio atender a **todas**:

- é o Galaxy S26+ (não o S26 comum, Ultra ou acessório);
- 256 GB ou 512 GB;
- cor diferente de branco e rosa;
- novo e lacrado (título, condição e descrição não podem indicar usado, seminovo, recondicionado, vitrine, caixa aberta, saúde de bateria etc.);
- vendido pela Samsung ou pela própria loja (Amazon, Magalu, Casas Bahia); no Mercado Livre, só a loja oficial Samsung;
- menor preço exibido (Pix ou cartão) abaixo do limite.

Para mudar a frequência, altere `intervalo_horas` no `config.json` e rode o `instalar.bat` de novo. Se uma loja ficar 3 verificações seguidas sem poder ser lida, chega um aviso no Telegram (e outro quando voltar).

## Segurança

O token e o ID ficam só em `segredos.env`, no seu PC, com permissão de leitura apenas para o seu usuário do Windows. Esse arquivo não vai para o GitHub (`.gitignore`).

## Limitações

- Só roda com o PC ligado e você logado. Se o PC estiver desligado na hora marcada, roda assim que ligar.
- Cupons aplicados só no carrinho não aparecem na página.
- As lojas podem bloquear acessos automáticos a qualquer momento.
