# Configuração de produção

O deployment do backend deve receber as configurações abaixo pela plataforma. Não armazene credenciais reais
no repositório.

- `CORS_ORIGINS`: origens autorizadas dos clientes web, incluindo o painel GeoMídia e o FORMS GEO, separadas por vírgula.
- `PUBLIC_FORM_ORIGINS`: domínio público definitivo do FORMS GEO. A origem é validada pelos endpoints públicos.
- `DATABASE_URL`: conexão usada pela aplicação.
- `DATABASE_DIRECT_URL`: conexão direta usada pelo Alembic, quando o `DATABASE_URL` utiliza pool transacional.
- `SUPABASE_URL`: URL do projeto Supabase.
- `SUPABASE_SERVICE_ROLE_KEY`: chave secreta `sb_secret_...` ou JWT legado com papel `service_role`.
- `SUPABASE_STORAGE_BUCKET`: nome do bucket privado de anexos; o padrão é `application-form-attachments`.
- `JWT_SECRET`: segredo forte e exclusivo do ambiente de produção.
- `ENVIRONMENT=production` e `CREATE_TABLES=false`.

O FORMS GEO deve receber, como variável de build:

```text
FORM_API_URL=https://geomidia-back.vercel.app/api
```

Antes do deployment, aplique com Alembic as migrations ainda pendentes usando `DATABASE_DIRECT_URL`. O bucket de
anexos deve existir como privado e as credenciais configuradas precisam permitir criar URLs assinadas, consultar
metadados dos objetos e criar URLs temporárias de download.

As origens devem conter apenas a origem (`https://dominio`, sem caminho). O domínio do FORMS GEO precisa estar
em `CORS_ORIGINS`, para o navegador aceitar as respostas, e em `PUBLIC_FORM_ORIGINS`, para o backend autorizar a
criação e a finalização de solicitações.
