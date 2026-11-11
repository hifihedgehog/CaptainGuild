"""The welcome picture: its design as data, the fonts and backgrounds it draws from, and the renderer that turns one into a PNG or a GIF.

Nothing in this package touches Discord or the database. The welcomecard module gathers what a picture needs, the member's avatar and the
server's uploads among it, and calls `render` on a worker thread.
"""
