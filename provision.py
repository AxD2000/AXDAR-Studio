"""Repair only requirements used by an AXDAR graph, on a Vast ComfyUI template."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen

from production import digest

SAGE_REVISION = "eb615cf6cf4d221338033340ee2de1c37fbdba4a"
SAGE_CHECK = """import torch
from sageattention import sageattn
from sageattention.core import get_cuda_arch_versions, attn_false, per_channel_fp8, per_thread_int8_triton, per_block_int8_triton, per_warp_int8_cuda
assert get_cuda_arch_versions()
if torch.cuda.get_device_capability() == (12, 0):
    from sageattention.core import sm89_compile
    assert hasattr(sm89_compile, 'qk_int8_sv_f8_accum_f32_fuse_v_scale_attn_inst_buf')
"""


def comfy_python():
    return os.environ.get("COMFY_PYTHON", "/venv/main/bin/python")


def sage_ready():
    return subprocess.run([comfy_python(), "-c", SAGE_CHECK],
                          capture_output=True, timeout=40).returncode == 0


def install_sage(root):
    """Build the pinned upstream wheel against the instance's installed Torch and CUDA."""
    py = comfy_python()
    with tempfile.TemporaryDirectory(dir=root.parent, prefix="axdar-sage-") as folder:
        path = Path(folder)
        source, helpers, wheels, staged = (path/name for name in ("source","helpers","wheels","staged"))
        subprocess.run(["git","init","-q",str(source)], check=True)
        def step(args, **kwargs):
            subprocess.run(args, check=True, capture_output=True, timeout=900, **kwargs)
        step(["git","-C",str(source),"fetch","--depth=1","https://github.com/thu-ml/SageAttention.git",SAGE_REVISION])
        step(["git","-C",str(source),"checkout","--detach",SAGE_REVISION])
        pip = [py,"-m","pip","--disable-pip-version-check"]
        step(pip+["install","--index-url","https://pypi.org/simple","--only-binary=:all:","--no-deps","--target",str(helpers),"ninja==1.13.0"])
        env = dict(os.environ, PYTHONPATH=str(helpers), PATH=str(helpers/"bin")+os.pathsep+os.environ["PATH"],
                   MAX_JOBS="6", EXT_PARALLEL="3")
        headers = subprocess.check_output([py,"-c","import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
        include = Path(headers)/"nvidia"/"cu13"/"include"
        if include.is_dir():
            env["CPATH"] = os.pathsep.join(filter(None,(str(include),env.get("CPATH"))))
        step(pip+["wheel","--no-deps","--no-build-isolation","--wheel-dir",str(wheels),str(source)], env=env)
        wheel, = wheels.glob("sageattention-*.whl")
        step(pip+["install","--no-index","--no-deps","--target",str(staged),str(wheel)])
        step([py,"-c",SAGE_CHECK+"\nx=torch.rand(1, 2, 128, 128, device='cuda', dtype=torch.float16)\nassert torch.isfinite(sageattn(x,x,x)).all()"],
             env=dict(os.environ,PYTHONPATH=str(staged)))
        step(pip+["install","--no-index","--no-deps",str(wheel)])

# Same projects used by the bundled setup.sh. Pin upstream revisions and limit pip
# to their reviewed public-package requirements. Never run repository install hooks.
NODE_SOURCES=[
    ({"MiniMaxChunkFeedForward","MiniMaxH3MemoryEfficientSageAttentionPatch","ResolutionSelector"}, "kijai/ComfyUI-KJNodes","56781c108c520e9c75db140b6886927c1785e3d2",["pillow>=10.3.0","color-matcher","matplotlib","mss","opencv-python-headless"]),
    ({"Lora Loader Stack (rgthree)"}, "rgthree/rgthree-comfy","bc48c6aaeca87bedbc14245d89f9bc6be37a630e",[]),
    ({"VHS_VideoCombine"}, "Kosinkadink/ComfyUI-VideoHelperSuite","4d907bee61e92c2e65af3bd6383a4e4d356126d1",["opencv-python","imageio-ffmpeg"]),
    ({"easy float"}, "yolain/ComfyUI-Easy-Use","d08ce763bc28e952566ecda91235709aa3fc6bbb",["diffusers","accelerate","clip_interrogator>=0.6.0","lark","onnxruntime","opencv-python-headless","sentencepiece","spandrel","matplotlib","peft"]),
    ({"MinimaxH3LatentUpscaler3D"}, "LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler","f34cadb19f10fee7e36091b690cac39b4eb40573",[])
]


def install_nodes(root,missing):
    installed=set()
    py=Path(comfy_python())
    for kinds,repo,rev,requirements in NODE_SOURCES:
        if not kinds & missing:continue
        dest=root/"custom_nodes"/repo.split("/")[1]
        if not dest.exists():
            dest.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(dir=dest.parent.parent) as staging:
                with_repo=Path(staging)/"source"
                subprocess.run(["git","init","-q",str(with_repo)],check=True)
                subprocess.run(["git","-C",str(with_repo),"fetch","--depth=1","https://github.com/"+repo+".git",rev],check=True,timeout=300)
                subprocess.run(["git","-C",str(with_repo),"checkout","--detach","FETCH_HEAD"],check=True)
                actual=subprocess.check_output(["git","-C",str(with_repo),"rev-parse","HEAD"],text=True).strip()
                if actual!=rev:raise ValueError("Revision mismatch in "+repo)
                with_repo.rename(dest)
        if requirements:
            if not py.is_file():raise ValueError("Set COMFY_PYTHON to the template's Python executable")
            subprocess.run([str(py),"-m","pip","install","--only-binary=:all:","--index-url","https://pypi.org/simple",*requirements],check=True,timeout=900)
        installed.update(kinds & missing)
    return installed


def install_models(root, required):
    lock=json.loads(Path(__file__).with_name("model-lock.json").read_text("utf-8"))
    known={Path(m["file"]).name:m for m in lock}
    changed=False
    for name in required:
        m=known.get(name.replace("\\","/").split("/")[-1])
        if not m:raise ValueError("No verified model source for "+name)
        dest=root/"models"/m["folder"]/Path(m["file"]).name
        dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.exists():
            if dest.stat().st_size!=m["bytes"] or digest(dest)!=m["sha256"]:raise ValueError("Existing model failed verification: "+str(dest))
            continue
        if shutil.disk_usage(dest.parent).free<m["bytes"]+256*1024**2:raise ValueError("Insufficient disk space for "+name)
        tmp=dest.with_suffix(".axdar-part")
        url="https://huggingface.co/"+m["repo"]+"/resolve/"+m["revision"]+"/"+m["file"]
        with urlopen(url,timeout=90) as src,tmp.open("wb") as out:
            shutil.copyfileobj(src,out,1024*1024)
        if tmp.stat().st_size!=m["bytes"] or digest(tmp)!=m["sha256"]:
            tmp.unlink()
            raise ValueError("Downloaded model failed verification: "+name)
        tmp.replace(dest)
        changed=True
    return changed

