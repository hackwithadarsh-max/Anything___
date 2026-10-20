AnyThing Downloader — fixed build
=================================

Files:
- backed.py  : Flask + yt-dlp backend; serves the frontend and API on one port.
- index.html : frontend with Light, Dark, Rain, Winter, and Summer themes.

ANDROID + TERMUX SETUP
----------------------
1. Put backed.py and index.html in the same folder, for example ~/anything.
2. Open Termux and run:

   pkg update && pkg upgrade
   pkg install python ffmpeg
   python -m pip install --upgrade pip
   python -m pip install --upgrade flask flask-cors yt-dlp waitress

3. Go to the folder containing both files:

   cd ~/anything

   (Use your actual folder path if different.)

4. Start the backend:

   python backed.py

5. Keep Termux open. In Chrome on the same phone, open:

   http://127.0.0.1:5000

   Do NOT open http://[::]:5000. Use 127.0.0.1 on the same phone.

6. Test backend health in a second browser tab:

   http://127.0.0.1:5000/api/health

   A healthy response contains "ok": true and a yt-dlp version.

TROUBLESHOOTING
---------------
- "Failed to fetch": ensure the terminal still shows the server running; open the site
  through http://127.0.0.1:5000, not by opening index.html directly and not through
  a separate static server. Check /api/health.
- "Address already in use": stop the old server with Ctrl+C, or find and stop the
  process using port 5000 before starting again.
- "Could not extract media": update yt-dlp (`python -m pip install -U yt-dlp`),
  retry a public URL that you own or are allowed to download, and check the Termux
  terminal for the actual extractor error. Some sites require login, block requests,
  or change their systems; no downloader can guarantee every link works.
- For merged high-resolution video and MP3 conversion, install ffmpeg (`pkg install ffmpeg`).
- Keep the app/backend updated. Do not expose this development server to the public internet.

USAGE
-----
Paste a supported public URL → Preview → choose an available format.
Only download media you own or have permission to save, and follow each platform's terms.
