#!/usr/bin/env python
from setuptools import setup

with open("README.rst") as fp:
    long_description = fp.read()

with open("jsonfield/version.txt") as fp:
    version = fp.read()

setup(
    name="django-jsonfield",
    version=version,
    description="JSONField for django models",
    long_description=long_description,
    long_description_content_type="text/x-rst",
    url="https://github.com/adamchainz/django-jsonfield",
    author="Matthew Schinckel",
    author_email="matt@schinckel.net",
    maintainer="Adam Johnson",
    maintainer_email="me@adamj.eu",
    packages=[
        "jsonfield",
    ],
    include_package_data=True,
    test_suite='tests.main',
    install_requires=[
        'Django>=1.8',
        'MySQL-python==1.2.5;python_version<"3.5"',
        'mysqlclient==1.3.14;python_version>="3.5"',
        'six',
    ],
    classifiers=[
        "Development Status :: 6 - Mature",
        'Framework :: Django',
        "Framework :: Django :: 1.8",
        "Framework :: Django :: 1.11",
        "Framework :: Django :: 2.0",
        "Framework :: Django :: 2.1",
        "Framework :: Django :: 2.2",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 3",
    ],
)
