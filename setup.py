import setuptools
import sys

with open("README.md", "r") as fh:
    long_description = fh.read()

install_requires=['numpy', 'scipy']
python_subversion = int(sys.version.split('.')[1])
if python_subversion > 8:
    install_requires = [
        'numpy', 'scipy', 'tensorflow', 'tensorflow-probability',
        'tensorflow-datasets', 'torch', 'torchvision', 'seaborn',
        'matplotlib', 'tensorflow-datasets'
    ]
else:
    install_requires = [
        'numpy==1.22.4', 'scipy',
        #####
        # these ought to be enforced by tensor2tensor, but they're not
        'kfac==0.2.0',
        'dopamine_rl==2.0.5',
        'gym==0.12.4',
        'absl-py==0.10.0',
        #####
        'tensorflow-probability==0.7',
        'tensor2tensor==1.15.7',
        'tfmpl',
        'protobuf==3.20.3',
        # 'tensorflow-gpu==1.15.3'  the cpu version will also work
    ]


setuptools.setup(
    name="machine_learning",
    version="0.7.0",
    author="J.G. Makin",
    author_email="jgmakin@gmail.com",
    description="a collection of packages for ML projects, written in the Python APIs for Tensorflow and Pytorch",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/jgmakin/machine_learning",
    packages=setuptools.find_packages(),
    install_requires=install_requires,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Science/Research",
        "Topic :: Scientific/Engineering",
        "Programming Language :: Python :: 3",
        # "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
