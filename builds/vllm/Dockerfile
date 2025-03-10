# FROM garrett4wade/real-gpu:23.11-py3-0.3.0
# RUN git clone --depth=1 -b v0.6.3.post1 https://github.com/vllm-project/vllm.git /vllm
# RUN apt install kmod ccache -y
# RUN cd /vllm && \
#     python3 use_existing_torch.py && \
#     pip3 install -r requirements-build.txt && \
#     MAX_JOBS=2 pip3 install -e . --no-build-isolation
# RUN yes | pip3 uninstall uvloop
# RUN pip3 install opencv-python-headless==4.5.4.58

# RUN apt-get update && apt-get install -y python3.10-venv

# RUN git clone --depth=1 https://github.com/QwenLM/Qwen2.5-Math /qwen2_5-math && mv /qwen2_5-math/evaluation/latex2sympy /latex2sympy
# RUN python3 -m venv /sympy
# RUN /sympy/bin/pip install /latex2sympy
# RUN /sympy/bin/pip install regex numpy tqdm datasets python_dateutil sympy==1.12 antlr4-python3-runtime==4.11.1 word2number Pebble timeout-decorator prettytable

FROM ghcr.io/inclusionai/areal-runtime:v0.1.0 

RUN pip3 install ninja
# Set TORCH_CUDA_ARCH_LIST if running and building on different GPU types


RUN cd / && git clone --depth=1 https://github.com/Dao-AILab/flash-attention && cd flash-attention/hopper && MAX_JOBS=1 python3 setup.py install
