# Alerta de preço — Galaxy S26+

Robô que verifica a cada hora o preço do **Samsung Galaxy S26+** na Amazon, Magazine Luiza, Mercado Livre e Casas Bahia, e avisa no **Telegram** quando aparece um anúncio abaixo de **R$ 4.900**.

## Regras aplicadas

Só gera alerta se o anúncio atender a **todas**:

- é o Galaxy S26+ (não o S26 comum, Ultra ou acessório);
- 256 GB ou 512 GB;
- cor diferente de branco e rosa;
- aparelho novo e lacrado: título, condição e descrição não podem indicar usado, seminovo, recondicionado, vitrine, caixa aberta, saúde de bateria etc.;
- vendido pela Samsung ou pela própria loja (Amazon, Magalu, Casas Bahia). No Mercado Livre, só a loja oficial Samsung;
- menor preço exibido (Pix ou cartão) abaixo do limite.

O mesmo anúncio só é avisado de novo se o preço cair ainda mais, ou se subir acima do limite e depois voltar a cair.

Se uma loja ficar 6 verificações seguidas sem poder ser lida (bloqueio ou mudança no site), chega um aviso no Telegram, e outro quando voltar a funcionar.

## Como usar

- **Testar agora:** aba *Actions* → *Alerta S26+* → *Run workflow*. Com "Enviar um resumo de teste" marcado, chega no Telegram um resumo do que foi lido em cada loja.
- **Mudar o preço, cores ou lojas:** edite `config.json`.
- **Pausar:** aba *Actions* → *Alerta S26+* → menu `…` → *Disable workflow*.

## Segurança

O token do bot e o ID do chat ficam apenas nos *Secrets* do repositório (Settings → Secrets and variables → Actions). Eles não aparecem no código, nos logs nem no `state.json`. O workflow não roda em pull requests de terceiros, então ninguém de fora consegue acessá-los.

## Limitações

- Cupons aplicados só no carrinho não aparecem na página, então o robô não os enxerga.
- As lojas podem bloquear acessos automáticos a qualquer momento; nesse caso você recebe o aviso descrito acima.
- O agendamento do GitHub pode atrasar alguns minutos nos horários de pico.
