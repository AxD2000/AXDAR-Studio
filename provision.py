"""Repair only requirements used by an AXDAR graph, on a Vast ComfyUI template."""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit
import re

from production import digest


MODEL_SUFFIXES = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".onnx", ".sft")
SAGE_REVISION = "eb615cf6cf4d221338033340ee2de1c37fbdba4a"
SAGE_CHECK = """import torch
from sageattention import sageattn
from sageattention.core import get_cuda_arch_versions, attn_false, per_channel_fp8, per_thread_int8_triton, per_block_int8_triton, per_warp_int8_cuda
assert get_cuda_arch_versions()
if torch.cuda.get_device_capability() == (12, 0):
    from sageattention.core import sm89_compile
    assert hasattr(sm89_compile, 'qk_int8_sv_f8_accum_f32_fuse_v_scale_attn_inst_buf')
"""


def model_path(name):
    return re.sub(r"[\\/]+", "/", name)


def model_choice(name, options):
    if name in options:
        return name
    path = model_path(name)
    matches = [option for option in options if isinstance(option, str) and model_path(option) == path]
    return matches[0] if len(matches) == 1 else None


def comfy_python():
    return os.environ.get("COMFY_PYTHON", "/venv/main/bin/python")


def requires_sage(graph):
    return any(node["class_type"] in ("MiniMaxH3MemoryEfficientSageAttentionPatch", "PathchSageAttentionKJ") for node in graph.values())


def sage_ready():
    return subprocess.run([comfy_python(), "-c", SAGE_CHECK],
                          capture_output=True, timeout=40).returncode == 0


def git_head(path):
    """Return the 40-char lowercase Git HEAD commit for a directory, or None."""
    path = Path(path)
    if not path.is_dir():
        return None
    try:
        out = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=15,
        ).strip().lower()
        return out if re.fullmatch(r"[0-9a-f]{40}", out) else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_is_clean(path):
    """Return True if path is a Git repo with no modified tracked files."""
    path = Path(path)
    if git_head(path) is None:
        return False
    try:
        out = subprocess.check_output(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            stderr=subprocess.DEVNULL, text=True, timeout=15,
        ).strip()
        return out == ""
    except (OSError, subprocess.SubprocessError):
        return False


def align_comfyui_revision(root, repo, expected_rev):
    """Explicitly check out a pinned ComfyUI commit on a clean Git checkout. Never mutates dirty repos."""
    root = Path(root)
    if not isinstance(expected_rev, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_rev):
        raise ValueError("ComfyUI revision must be a pinned 40-char commit SHA.")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid ComfyUI repository name.")
    actual = git_head(root)
    if actual == expected_rev:
        return False
    if actual is None:
        raise ValueError("ComfyUI directory is not a Git repository; cannot align revision automatically.")
    if not git_is_clean(root):
        raise ValueError("ComfyUI repository has local uncommitted changes; refusing to overwrite them automatically.")
    url = "https://github.com/" + repo + ".git"
    subprocess.run(["git", "-C", str(root), "fetch", "--depth=1", url, expected_rev], check=True, timeout=300)
    subprocess.run(["git", "-C", str(root), "checkout", "--detach", "FETCH_HEAD"], check=True, timeout=120)
    verified = git_head(root)
    if verified != expected_rev:
        raise ValueError(f"ComfyUI alignment failed: expected {expected_rev}, got {verified}.")
    return True


def installed_pip_packages(py=None):
    py = str(py or comfy_python())
    try:
        out = subprocess.check_output(
            [py, "-m", "pip", "list", "--format=json"],
            stderr=subprocess.DEVNULL, text=True, timeout=60,
        )
        return {item["name"].lower().replace("_", "-"): item["version"] for item in json.loads(out)}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def install_sage(root, revision=SAGE_REVISION):
    """Build the pinned upstream wheel against the instance's installed Torch and CUDA."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("SageAttention revision must be a pinned 40-char commit SHA.")
    py = comfy_python()
    with tempfile.TemporaryDirectory(dir=root.parent, prefix="axdar-sage-") as folder:
        path = Path(folder)
        source, helpers, wheels, staged = (path/name for name in ("source","helpers","wheels","staged"))
        subprocess.run(["git","init","-q",str(source)], check=True)
        def step(args, **kwargs):
            print("[sage]", " ".join(str(a) for a in args[:6]), flush=True)
            subprocess.run(args, check=True, timeout=900, **kwargs)
        step(["git","-C",str(source),"fetch","--depth=1","https://github.com/thu-ml/SageAttention.git",revision])
        step(["git","-C",str(source),"checkout","--detach",revision])
        actual = git_head(source)
        if actual != revision:
            raise ValueError(f"SageAttention revision mismatch: expected {revision}, got {actual}.")
        pip = [py,"-m","pip","--disable-pip-version-check"]
        step(pip+["install","--index-url","https://pypi.org/simple","--only-binary=:all:","--no-deps","--target",str(helpers),"ninja==1.13.0"])
        env = dict(os.environ, PYTHONPATH=str(helpers), PATH=str(helpers/"bin")+os.pathsep+os.environ["PATH"],
                   MAX_JOBS="2", EXT_PARALLEL="1")
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
    ({"MiniMaxChunkFeedForward","MiniMaxH3MemoryEfficientSageAttentionPatch","PathchSageAttentionKJ","ResolutionSelector"}, "kijai/ComfyUI-KJNodes","56781c108c520e9c75db140b6886927c1785e3d2",["pillow>=10.3.0","color-matcher","matplotlib","mss","opencv-python-headless"]),
    ({"DenoMiniMaxH3ReferenceToVideo","DenoMiniMaxH3ReferenceImageLoader","DenoResolutionSetup","DenoTextEncoderUnload"}, "Deno2026/comfyui-deno-custom-nodes","b43184f41d5aca84808f37da220ccceba4b48bde",[]),
    ({"Lora Loader Stack (rgthree)"}, "rgthree/rgthree-comfy","bc48c6aaeca87bedbc14245d89f9bc6be37a630e",[]),
    ({"VHS_VideoCombine","VHS_LoadVideo"}, "Kosinkadink/ComfyUI-VideoHelperSuite","4d907bee61e92c2e65af3bd6383a4e4d356126d1",["opencv-python","imageio-ffmpeg"]),
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
            pip=[str(py),"-m","pip","install","--only-binary=:all:","--index-url","https://pypi.org/simple"]
            freeze=subprocess.check_output([str(py),"-m","pip","list","--format=json"],text=True,timeout=60)
            protected=("torch","torchvision","torchaudio","triton","xformers")
            with tempfile.TemporaryDirectory() as temp:
                constraints=Path(temp)/"gpu-constraints.txt"
                constraints.write_text("".join(f"{item['name']}=={item['version']}\n" for item in json.loads(freeze)
                                               if item["name"].lower() in protected or item["name"].lower().startswith("nvidia-")),"utf-8")
                subprocess.run([*pip,"--constraint",str(constraints),*requirements],check=True,timeout=900)
        installed.update(kinds & missing)
    return installed


class ModelRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def model_sources():
    lock=json.loads(Path(__file__).with_name("model-lock.json").read_text("utf-8"))
    return {Path(item["file"]).name:item for item in lock}


def download_weight(url, tmp, expected, progress=None, label=""):
    offset=tmp.stat().st_size if tmp.exists() else 0
    if offset == expected:
        return
    headers={}
    if token:=os.environ.get("HF_TOKEN"):
        headers["Authorization"]="Bearer "+token
    if offset:
        headers["Range"]=f"bytes={offset}-"
    name = label or tmp.stem
    started = time.monotonic()
    start_offset = offset
    last_report = 0.0
    print(f"[download] Starting {name} ({offset/1e9:.2f}/{expected/1e9:.2f} GB)", flush=True)
    with build_opener(ModelRedirect()).open(Request(url,headers=headers),timeout=90) as src:
        if src.status==206:
            match=re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",src.headers.get("Content-Range",""))
            if not match or int(match[1])!=offset or int(match[3])!=expected:
                raise ValueError("The model server returned an invalid resume range.")
        else:
            offset=0
            start_offset=0
        with tmp.open("ab" if offset else "wb") as out:
            while chunk:=src.read(1024*1024):
                offset+=len(chunk)
                if offset>expected:
                    raise ValueError("The download exceeds the verified model size.")
                out.write(chunk)
                now = time.monotonic()
                if now - last_report >= 2.0 or offset == expected:
                    elapsed = max(0.001, now - started)
                    mb_s = (offset - start_offset) / (1024 * 1024) / elapsed
                    pct = (offset * 100.0 / expected) if expected else 100.0
                    print(f"[download] {name}: {offset/1e9:.2f}/{expected/1e9:.2f} GB ({pct:.1f}%) · {mb_s:.1f} MiB/s", flush=True)
                    if progress:
                        progress(offset, expected, mb_s)
                    last_report = now


def install_package_nodes(root, node_specs):
    """Install strictly pinned custom node repositories declared by a workflow package."""
    installed = []
    py = Path(comfy_python())
    for spec in node_specs:
        repo = spec["repo"]
        rev = spec["revision"]
        requirements = spec.get("requirements", [])
        dest = root / "custom_nodes" / repo.split("/")[1]
        if dest.exists():
            actual = git_head(dest)
            if actual != rev:
                raise ValueError(
                    f"Incompatible custom node revision for {repo}: found {actual or 'untracked'}, expected {rev}."
                )
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=dest.parent.parent) as staging:
                with_repo = Path(staging) / "source"
                subprocess.run(["git", "init", "-q", str(with_repo)], check=True)
                subprocess.run(["git", "-C", str(with_repo), "fetch", "--depth=1", "https://github.com/" + repo + ".git", rev], check=True, timeout=300)
                subprocess.run(["git", "-C", str(with_repo), "checkout", "--detach", "FETCH_HEAD"], check=True)
                actual = git_head(with_repo)
                if actual != rev:
                    raise ValueError(f"Revision mismatch in {repo}: expected {rev}, got {actual}.")
                with_repo.rename(dest)
            installed.append(repo)
        if requirements:
            if not py.is_file():
                raise ValueError("Set COMFY_PYTHON to the template's Python executable.")
            pip = [str(py), "-m", "pip", "install", "--only-binary=:all:", "--index-url", "https://pypi.org/simple"]
            freeze = subprocess.check_output([str(py), "-m", "pip", "list", "--format=json"], text=True, timeout=60)
            protected = ("torch", "torchvision", "torchaudio", "triton", "xformers")
            with tempfile.TemporaryDirectory() as temp:
                constraints = Path(temp) / "gpu-constraints.txt"
                constraints.write_text(
                    "".join(
                        f"{item['name']}=={item['version']}\n"
                        for item in json.loads(freeze)
                        if item["name"].lower() in protected or item["name"].lower().startswith(("nvidia-", "cuda-"))
                    ),
                    "utf-8",
                )
                subprocess.run([*pip, "--constraint", str(constraints), *requirements], check=True, timeout=900)
    return installed


def verify_model_file(root, spec, *, check_hash=True):
    """Strictly verify a locked model at its exact target path with zero substitution."""
    dest = root / "models" / spec["folder"] / Path(spec["file"]).name
    if not dest.exists():
        return "Missing", dest, ""
    if not dest.is_file():
        return "Incompatible", dest, "Target path is not a regular file."
    actual_size = dest.stat().st_size
    if actual_size != spec["bytes"]:
        return "Incompatible", dest, f"Size mismatch: found {actual_size} bytes, expected {spec['bytes']}."
    if check_hash:
        actual_sha = digest(dest)
        if actual_sha != spec["sha256"]:
            return "Incompatible", dest, f"SHA-256 mismatch at {dest.name}."
    return "Installed", dest, ""


def install_package_models(root, model_specs, progress=None):
    """Download and SHA-256 verify strictly locked models declared by a workflow package."""
    changed = False
    for m in model_specs:
        status, dest, reason = verify_model_file(root, m, check_hash=True)
        if status == "Installed":
            continue
        if status == "Incompatible":
            raise ValueError(f"Existing model failed strict verification ({dest}): {reason}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".axdar-part")
        partial = tmp.stat().st_size if tmp.exists() else 0
        if shutil.disk_usage(dest.parent).free < max(0, m["bytes"] - partial) + 256 * 1024**2:
            raise ValueError("Insufficient disk space for " + Path(m["file"]).name)
        url = "https://huggingface.co/" + m["repo"] + "/resolve/" + m["revision"] + "/" + m["file"]
        label = Path(m["file"]).name
        download_weight(url, tmp, m["bytes"], progress=progress, label=label)
        if tmp.stat().st_size != m["bytes"]:
            raise ValueError("Model download incomplete. Retry to resume: " + label)
        print(f"[verify] Checking SHA-256 for {label}...", flush=True)
        if digest(tmp) != m["sha256"]:
            tmp.unlink()
            raise ValueError("Downloaded model failed SHA-256 verification: " + label)
        tmp.replace(dest)
        print(f"[verify] Verified {label}", flush=True)
        changed = True
    return changed


def install_models(root, required, progress=None):
    known=model_sources()
    changed=False
    for name in required:
        m=known.get(name.replace("\\","/").split("/")[-1])
        if not m:raise ValueError("No verified model source for "+name)
        dest=root/"models"/m["folder"]/Path(m["file"]).name
        dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.exists():
            if dest.stat().st_size!=m["bytes"] or digest(dest)!=m["sha256"]:raise ValueError("Existing model failed verification: "+str(dest))
            continue
        tmp=dest.with_suffix(".axdar-part")
        partial=tmp.stat().st_size if tmp.exists() else 0
        if shutil.disk_usage(dest.parent).free<max(0,m["bytes"]-partial)+256*1024**2:
            raise ValueError("Insufficient disk space for "+name)
        url="https://huggingface.co/"+m["repo"]+"/resolve/"+m["revision"]+"/"+m["file"]
        download_weight(url,tmp,m["bytes"],progress=progress,label=name)
        if tmp.stat().st_size!=m["bytes"]:
            raise ValueError("Model download incomplete. Retry to resume: "+name)
        print(f"[verify] Checking SHA-256 for {name}...", flush=True)
        if digest(tmp)!=m["sha256"]:
            tmp.unlink()
            raise ValueError("Downloaded model failed verification: "+name)
        tmp.replace(dest)
        print(f"[verify] Verified {name}", flush=True)
        changed=True
    return changed
