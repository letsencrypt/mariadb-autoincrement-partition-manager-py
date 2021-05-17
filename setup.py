from setuptools import setup


setup(
    name="mariadb-sequential-partition-manager",
    version="0.2.0",
    description="Manage DB partitions based on sequential IDs",
    long_description="Manage MariaDB Partitions based on sequential IDs",
    classifiers=[
        "Development Status :: 4 - Beta",
        "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)",
        "Programming Language :: Python :: 3",
    ],
    keywords="database",
    url="http://github.com/letsencrypt/mariadb-sequential-partition-manager",
    author="J.C. Jones",
    author_email="jc@letsencrypt.org",
    license="Mozilla Public License 2.0 (MPL 2.0)",
    zip_safe=False,
    include_package_data=True,
    python_requires=">=3.6",
    install_requires=["PyMySQL >= 1.0.2", "pyyaml"],
    packages=["partitionmanager"],
    entry_points={"console_scripts": ["partition-manager=partitionmanager.cli:main"]},
)
