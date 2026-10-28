from __future__ import annotations
import os, shutil, tempfile, time
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent.resolve()
JOBS_DIR = Path(tempfile.gettempdir()) / "logradouros_jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
EXT_SHP_OK = (".zip", ".shp", ".gpkg", ".geojson")

app = FastAPI(title="Extrator de Logradouros")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

def _limpar_antigos(max_horas: int = 2) -> None:
    limite = time.time() - max_horas * 3600
    for d in JOBS_DIR.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < limite: shutil.rmtree(d, ignore_errors=True)
        except Exception: pass

@app.get("/health")
def health(): return {"status": "ok"}

@app.get("/")
def index(): return FileResponse(str(BASE_DIR / "static" / "index.html"))

@app.post("/processar")
def processar(
    pdf: UploadFile = File(...),
    shapefile: UploadFile = File(...),
    osm: UploadFile = File(None),
    municipio: str = Form(...),
    uf: str = Form(...)
):
    _limpar_antigos()

    if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "O PDF deve terminar em '.pdf'.")
    if not shapefile.filename or not shapefile.filename.lower().endswith(EXT_SHP_OK):
        raise HTTPException(400, "Shapefile deve ser .zip, .shp, .gpkg ou .geojson.")
    
    municipio, uf = municipio.strip(), uf.strip().upper()
    if not municipio or len(uf) != 2 or not uf.isalpha():
        raise HTTPException(400, "Municipio ou UF invalidos.")

    job_dir = JOBS_DIR / f"job_{int(time.time()*1000)}_{os.getpid()}"
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = job_dir / "mapa.pdf"
    shp_path = job_dir / shapefile.filename
    out_dir = job_dir / "output"
    out_dir.mkdir(exist_ok=True)

    with open(pdf_path, "wb") as f: shutil.copyfileobj(pdf.file, f)
    with open(shp_path, "wb") as f: shutil.copyfileobj(shapefile.file, f)

    osm_path = None
    if osm and osm.filename:
        osm_path = str(job_dir / osm.filename)
        with open(osm_path, "wb") as f: shutil.copyfileobj(osm.file, f)

    try:
        from extrair_logradouros import processar_pipeline
        zip_path = processar_pipeline(
            pdf_path=str(pdf_path),
            shp_path=str(shp_path),
            municipio=municipio,
            uf=uf,
            out_dir=str(out_dir),
            crs="EPSG:31984",
            osm_path=osm_path
        )
    except Exception as e:
        raise HTTPException(500, f"Erro: {type(e).__name__}: {e}")

    return FileResponse(zip_path, media_type="application/zip", filename=Path(zip_path).name)
