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
    assert "concurrencia:" in t and 'default: "4"' in t.split("concurrencia:")[1][:500]
    assert "CNV_CONCURRENCIA: ${{ github.event.inputs.concurrencia || '4' }}" in t
    job = t[t.index("\n  indice:"):]
    assert job.index("CNV_CONCURRENCIA") < job.index("steps:")          # esta en el env del job: lo ven todos los pasos con modelo
