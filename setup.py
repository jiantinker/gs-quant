"""
Copyright 2018 Goldman Sachs.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""

import os
import setuptools
import shutil
import subprocess
import sys
from pathlib import Path

if "sdist" in sys.argv:
    reference = os.path.dirname(__file__)
    doc_dir = os.path.join(reference, "docs")
    p = subprocess.Popen(["make", "html"], cwd=doc_dir, shell=True)
    p.wait(30)
    if p.returncode != 0:
        raise RuntimeError("unable to make docs")

    generated_dir = Path(os.path.join(doc_dir, "_build", "html", "functions"))
    generated_dir.mkdir(parents=True, exist_ok=True)
    target_dir = os.path.join(reference, "gs_quant", "docs")
    shutil.rmtree(target_dir, ignore_errors=True)
    shutil.copytree(generated_dir, target_dir)

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="gs_quant",
    version="0.8.70",
    author="Goldman Sachs",
    author_email="developer@gs.com",
    description="Goldman Sachs Quant",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://marquee.gs.com",
    license="http://www.apache.org/licenses/LICENSE-2.0",
    packages=setuptools.find_packages(),
    include_package_data=True,
    # TODO: remove compatibility packages (configparser, future, six) now that we only support 3.6+
    install_requires=[
        "backoff",
        "cachetools",
        "configparser",
        "funcsigs",
        "future",
        "inflection",
        "msgpack",
        "pandas",
        "python-dateutil>=2.7.0",
        "requests",
        "scipy",
        "six",
        "typing;python_version<'3.7'"
    ],
    extras_require={
        "internal": ["gs_quant_internal>=0.4.1", "requests_kerberos"],
        "notebook": ["jupyter", "matplotlib~=2.1.0", "pprint"],
        "test": ["pytest", "pytest-cov", "pytest-mock", "testfixtures"],
        "develop": ["wheel", "sphinx", "sphinx_rtd_theme", "sphinx_autodoc_typehints", "pytest", "pytest-cov",
                    "pytest-mock", "testfixtures"]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Operating System :: OS Independent",
        "License :: OSI Approved :: Apache Software License"
    ],
)
