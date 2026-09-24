# skills-training

Learning notebooks for ML frameworks, written as guided walkthroughs with beginner-friendly markdown between code cells.

## Notebooks

- `ML/ExploreTF.ipynb`: TensorFlow. Sections: setup, tensors, NumPy interop, Variables, GradientTape, manual linear-regression loop (fits `y = 2x + 1`), then Keras `Sequential`/`Dense`, SGD vs Adam, `tf.data`, and an MNIST classifier (~97.7% test accuracy), ending with "Where to go from here".
- `ML/ExplorePyTorch.ipynb`: PyTorch, deliberately mirroring the TF notebook so the two can be compared side by side. It uses the same seed and data for the linear-regression loop, and the results match TF exactly (`w=1.926, b=1.006`). It frames each section around how PyTorch differs from TF: mutable tensors, autograd merging Variable + GradientTape, gradient accumulation and `.zero_()`, no `.fit()`, `CrossEntropyLoss` expecting logits. It ends at an MNIST classifier (~97.7%).

Both notebooks are complete. Natural next topics (listed in the notebooks' closing cells): CNNs (`Conv2D`/`Conv2d`), regularization, model saving, TensorBoard, custom Datasets, PyTorch Lightning.

## Environment

- Conda env **`TensorFlow`**: `C:\Users\Administrator\anaconda3\envs\TensorFlow\python.exe`. It has TensorFlow 2.21 (CPU only; native Windows has no GPU support for TF ≥ 2.11), PyTorch + torchvision, matplotlib, and nbconvert. Both notebooks use this env's Jupyter kernel, also named `TensorFlow`.
- The system `python` (3.10) is a different interpreter. Don't install notebook deps there.

## Conventions

- Explain each concept in plain-language markdown *before* the code cell that uses it. The user specifically asked for more plain-language text.
- To verify a notebook runs end to end (this also saves outputs into the file):
  `& "C:\Users\Administrator\anaconda3\envs\TensorFlow\python.exe" -m jupyter nbconvert --to notebook --execute --inplace <notebook>`
- MNIST data is committed under `ML/data/MNIST/raw/` (downloaded by torchvision). Don't commit further large datasets; GitHub rejects files over 100 MB.
- The `.gitignore` is the stock Visual Studio template and doesn't ignore notebook checkpoints (`.ipynb_checkpoints/`) or `ML/data/`.
