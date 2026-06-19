"""
Extract YouTube cookies from Chrome and write to cookies.txt (Netscape format).
This will trigger a macOS Keychain prompt — click "Allow" or "Always Allow".
"""
import os
import sys
import sqlite3
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from http.cookiejar import MozillaCookieJar, Cookie

def extract_chrome_cookies():
    """Try extracting cookies using browser_cookie3."""
    try:
        import browser_cookie3
        print("🔑 Extracting Chrome cookies (click 'Allow' on Keychain prompt)...")
        cj = browser_cookie3.chrome(domain_name=".youtube.com")
        return cj
    except Exception as e:
        print(f"❌ browser_cookie3 failed: {e}")
        return None

def write_cookies_txt(cookie_jar, output_path):
    """Write cookies to Netscape cookies.txt format."""
    lines = ["# Netscape HTTP Cookie File", "# https://curl.haxx.se/docs/http-cookies.html", ""]
    
    count = 0
    for cookie in cookie_jar:
        domain = cookie.domain
        if not domain:
            continue
        
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = cookie.path or "/"
        secure = "TRUE" if cookie.secure else "FALSE"
        expires = str(int(cookie.expires)) if cookie.expires else "0"
        name = cookie.name
        value = cookie.value or ""
        
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
        count += 1
    
    with open(output_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    
    return count

def main():
    output = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    
    # Try Chrome
    cj = extract_chrome_cookies()
    
    if cj:
        count = write_cookies_txt(cj, output)
        size = os.path.getsize(output)
        print(f"✅ Wrote {count} YouTube cookies to cookies.txt ({size} bytes)")
        print(f"📁 Path: {output}")
    else:
        print("\n💡 Alternative: Install 'Get cookies.txt LOCALLY' Chrome extension")
        print("   1. Go to youtube.com (logged in)")
        print("   2. Click the extension → Export")
        print("   3. Save as cookies.txt in this folder")
        sys.exit(1)

if __name__ == "__main__":
    main()
