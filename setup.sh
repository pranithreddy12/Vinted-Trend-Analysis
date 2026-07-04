#!/bin/bash
echo "==================================================="
echo "Vinted Scraper - Setup Script"
echo "==================================================="
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null
then
    echo "[ERROR] Python 3 is not installed."
    echo "Please download and install Python from https://www.python.org/downloads/mac-osx/"
    echo ""
    exit 1
fi

echo "[1/2] Installing required Python packages..."
python3 -m pip install -r requirements.txt

echo ""
echo "[2/2] Setting up Browser Automation (Playwright)..."
python3 -m playwright install chromium

echo ""
echo "==================================================="
echo "Setup Complete!"
echo "You can now run 'sh start_scraper.sh' to start the program."
echo "==================================================="
