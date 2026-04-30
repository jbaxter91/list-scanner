with open('build.bat','rb') as f:
    d=f.read()
print('CRLF' if b'\\r\\n' in d else 'LF-only')