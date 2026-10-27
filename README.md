# Extrator de Logradouros de Mapa PDF Municipal

Extrai nomes de logradouros de um PDF de arruamento (gerado em AutoCAD, SEM
coordenadas), georreferencia **todos** via modelo afim RANSAC calibrado com
âncoras do shapefile oficial (prioritário) e do OSM (complementar), e entrega
um GeoPackage auditável com 5 camadas.

## Estrutura de arquivos

```
extrator-logradouros/
├── app.py                     # FastAPI (endpoints /, /health, /processar)
├── extrair_logradouros.py     # núcleo + CLI
├── requirements.txt
├── Dockerfile
├── iniciar_servidor.bat       # atalho Windows
├── README.md
└── static/
    └── index.html             # frontend
```

> **Importante:** `index.html` precisa ficar dentro da subpasta `static/`.
> Se você baixou o Gist como ZIP, mova o `index.html` para `static/` antes de rodar.

## Instalação

### Linux/macOS
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8080
```

### Windows
Duplo-clique em `iniciar_servidor.bat` — cria venv, instala deps e sobe o servidor.

### Docker
```bash
docker build -t extrator-logradouros .
docker run --rm -p 8080:8080 extrator-logradouros
```

## Uso — Web
Abra <http://localhost:8080>, envie:
- **PDF** do mapa municipal;
- **ZIP** com o shapefile completo (`.shp .dbf .shx .prj`);
- **Município** e **UF**.

Retorna um ZIP com:
- `logradouros_<municipio>_FINAL.gpkg` — 5 camadas
- `logradouros.csv`
- `relatorio.txt`
- `modelo_afim.json`

## Uso — CLI
```bash
python extrair_logradouros.py mapa.pdf municipio.zip \
    --municipio Cajazeiras --uf PB --out ./saida
```

## Camadas do GeoPackage

| Camada | Conteúdo |
|---|---|
| `1_match_shapefile` | Match exato PDF ↔ shapefile |
| `2_match_osm` | Match exato PDF ↔ OSM |
| `3_match_fuzzy` | Match por similaridade (≥ 0.80) |
| `4_georreferenciados` | Sem match, mas com ponto projetado pelo modelo afim |
| `5_rejeitados_auditoria` | Candidatos descartados (para revisão) |

Colunas comuns: `logradouro, nome_fonte, fonte, origem, confianca, data,
dist_ancora_m, score, geometry`.

## Decisões técnicas

- **pymupdf** (`get_text("dict")`) e não pdfplumber — este último embaralha
  rótulos rotacionados típicos de PDFs SIG.
- **RANSAC** e não regressão linear simples — robusto a outliers (nomes
  homônimos, distorções locais do PDF).
- **Shapefile prioritário** sobre OSM — qualidade oficial > OSM em municípios
  pequenos.
- **Deduplicação** por geometria **+ similaridade de nome** (≥ 0.75) — não
  descartar ruas legítimas com nomes diferentes na mesma linha.
- **Matching com e sem prefixo** ("RUA X" vs "X") — tolera shapefiles/OSM que
  omitem o tipo.
- **Overpass alternativo** (`overpass.private.coffee`) + timeout 300s.
- **Fallback gracioso** do OSM: se cair, processa só com shapefile e registra
  no relatório.

## Classificação de confiança

- **alta** — match exato e `dist_ancora_m < 100 m`.
- **média** — fuzzy ≥ 0.85 e distância < 500 m.
- **baixa** — fuzzy entre 0.80–0.85, ou distância entre 100 m e 2 km.
- **ausente_georreferenciado** — sem match; posição estimada pelo modelo afim.
- **rejeitar** — distância > 2 km, ou erro mediano do modelo > 500 m.

## Limitações / troubleshooting

- **Overpass instável:** o código usa `overpass.private.coffee`. Se falhar,
  siga com o shapefile (fallback automático).
- **Shapefile com encoding errado:** o leitor tenta `pyogrio` → `fiona` →
  `fiona latin-1`.
- **CRS diferente:** `.to_crs("EPSG:31984")` é aplicado logo após a leitura.
  Para outras UTM: `--crs EPSG:31983` etc.
- **Poucas âncoras (< 3):** o modelo afim não é calibrado e os ausentes são
  rejeitados. Nesse caso, melhore o shapefile ou rode com OSM disponível.
- **Erro mediano > 500 m:** modelo ruim (PDF rotacionado, escala inconsistente,
  CRS errado). O relatório avisa; audite antes de usar.
- **PDF multipágina:** funciona (todas as páginas são processadas).