name: ML Experiment (تجريبي)

on:
  workflow_dispatch: {}

jobs:
  ml-experiment:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install pandas scikit-learn requests

      - name: Build dataset from Gist
        env:
          GIST_TOKEN: ${{ secrets.GIST_TOKEN }}
          GIST_ID: ${{ secrets.GIST_ID }}
        run: python build_dataset.py

      - name: Train model and show results
        run: python train_model.py

      - name: Upload dataset.csv as artifact
        uses: actions/upload-artifact@v4
        with:
          name: ml-dataset
          path: dataset.csv
