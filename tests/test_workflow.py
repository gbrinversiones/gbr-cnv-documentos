"""Chequeos de texto sobre sincronizar.yml (sin PyYAML: no esta en las dependencias del CI)."""


def workflow() -> str:
    return open(".github/workflows/sincronizar.yml", encoding="utf-8").read()


def test_input_completar_campos_llega_al_paso_financieros():
    t = workflow()
    assert "completar_campos:" in t and "default: false" in t.split("completar_campos:")[1][:600]
    assert "COMPLETAR_CAMPOS: ${{ github.event.inputs.completar_campos || 'false' }}" in t
    paso = t[t.index("- name: Extraer cifras de estados contables"):]
    paso = paso[:paso.index("\n      - name:", 10)]
    assert '[ "$COMPLETAR_CAMPOS" = "true" ]' in paso and "extra=(--completar-campos)" in paso and '"${extra[@]}"' in paso
    assert "--max $((150*K))" in paso            # el tope y el cupo no cambian


def test_sin_el_input_el_comando_no_lleva_el_flag():
    """Con el default (false) `extra` queda vacio: el comando es el de siempre."""
    import subprocess

    paso = 'extra=(); if [ "false" = "true" ]; then extra=(--completar-campos); fi; echo X "${extra[@]}" Y'
    assert subprocess.run(["bash", "-e", "-c", paso], capture_output=True, text=True).stdout.split() == ["X", "Y"]
    paso = paso.replace('"false"', '"true"')
    assert subprocess.run(["bash", "-e", "-c", paso], capture_output=True, text=True).stdout.split() == ["X", "--completar-campos", "Y"]


def test_la_concurrencia_se_configura_desde_el_workflow():
    t = workflow()
    assert "concurrencia:" in t and 'default: "2"' in t.split("concurrencia:")[1][:500]          # 2 mientras la clave se comparta con produccion
    assert "CNV_CONCURRENCIA: ${{ github.event.inputs.concurrencia || '2' }}" in t
    assert "tpm:" in t and 'default: "150000"' in t.split("  tpm:")[1][:500] and "CNV_TPM: ${{ github.event.inputs.tpm || '150000' }}" in t
    job = t[t.index("\n  indice:"):]
    assert job.index("CNV_CONCURRENCIA") < job.index("steps:")          # esta en el env del job: lo ven todos los pasos con modelo


def test_series_es_un_paso_final_del_job_indice_sin_modelo():
    t = workflow()
    job = t[t.index("\n  indice:"):]
    paso = job[job.index("- name: Series historicas"):]
    paso = paso[:paso.index("\n      - name:", 10)]
    assert "python -m cnv series" in paso and "ANTHROPIC_API_KEY" not in paso and "if: always()" in paso and "continue-on-error: true" in paso
    assert job.index("python -m cnv exportar") < job.index("python -m cnv series") < job.index("python -m cnv tiempos")
