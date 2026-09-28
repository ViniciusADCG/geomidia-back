# GeoMídia backend

## Comprovante do processo novo

Depois que `/api/public/solicitacoes/veiculos-divulgacao/{rascunhoId}/finalizar` cria o processo, o backend devolve um protocolo `VEI-01-2026` (ou o próximo número daquele ano). O comprovante pode ser baixado por `POST /api/public/solicitacoes/veiculos-divulgacao/{rascunhoId}/comprovante` com `{ "token": "..." }`. O PDF segue o modelo `VEI-0044-2026.pdf` fornecido com o formulário e usa os dados persistidos. Os anexos ficam no armazenamento privado; por isso, o comprovante lista seus nomes em vez de links temporários que expirariam.

O envio por e-mail está inativo nos dois fluxos públicos. A conclusão retorna `comprovanteEnviado: false` e apresenta o botão de download. Uma nova chamada a `/finalizar` com o mesmo rascunho e token devolve o protocolo existente sem criar outro processo.

As funções SMTP permanecem no código para uma ativação futura. Quando esse envio for retomado, configure no ambiente do backend, sem expor os valores no JavaScript do formulário:

```text
SMTP_HOST=smtp.exemplo.gov.br
SMTP_PORT=587
SMTP_FROM_EMAIL=protocolos@exemplo.gov.br
SMTP_USERNAME=usuario-do-servico
SMTP_PASSWORD=senha-ou-token-do-servico
SMTP_USE_STARTTLS=true
SMTP_USE_SSL=false
```

`SMTP_USERNAME` e `SMTP_PASSWORD` podem ficar ausentes se o servidor SMTP autorizado não exigir autenticação. Para SMTP com SSL na porta 465, use `SMTP_USE_SSL=true` e `SMTP_PORT=465`. As configurações SMTP não disparam mensagens no fluxo atual.

## Resposta de comunicado de exigência

O formulário inicia a resposta em `POST /api/public/solicitacoes/veiculos-divulgacao/exigencias/iniciar`, envia de 1 a 10 anexos pelas URLs assinadas e conclui em `POST /api/public/solicitacoes/veiculos-divulgacao/exigencias/{rascunhoId}/finalizar`. A conclusão registra os dados e os anexos no banco e devolve um protocolo `HESP-01-2026` (ou o próximo número daquele ano).

O comprovante baseado em `HESP-0272-2026.pdf` pode ser baixado por `POST /api/public/solicitacoes/veiculos-divulgacao/exigencias/{rascunhoId}/comprovante` com `{ "token": "..." }`. O download exige o token do rascunho finalizado. O PDF lista os nomes dos anexos, pois as URLs temporárias de upload não servem como links públicos permanentes.

Aplique as migrações Alembic até `20260928_0014` antes da publicação. Os contadores `VEI` e `HESP` são independentes, começam em `01` em um ano sem registros e passam a usar o novo ano na virada do calendário de Campo Grande. Protocolos já criados mantêm seus números; se houver números anteriores naquele ano, a sequência continua a partir do maior.

As respostas finalizadas permanecem em `public_submission_drafts`, com seus dados e metadados de anexos. O painel consulta `GET /api/requirement-responses` (busca e paginação), baixa os anexos por URL assinada em `GET /api/requirement-responses/{id}/attachments/{index}/download` e gera novamente o PDF em `GET /api/requirement-responses/{id}/comprovante`. Esses endpoints exigem login com perfil `analyst` ou `admin`; o perfil `viewer` não pode acessá-los.
