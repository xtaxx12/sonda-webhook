import socket, struct
IP   = "192.168.3.157"   # <-- la IP del módulo
PORT = 8899

req = struct.pack(">HHHBBHH", 1, 0, 6, 1, 3, 0, 6)
s = socket.create_connection((IP, PORT), timeout=6)
s.sendall(req); r = s.recv(256); s.close()

print("RAW:", r.hex(" "))
if len(r) >= 9 and r[7] == 3:
    n = r[8]
    regs = struct.unpack(">%dH" % (n//2), r[9:9+n])
    print("Registros:", regs)
    nom = ["Oxígeno disuelto (mg/L)", "Temperatura (°C)", "Saturación (%)"]
    for i in range(0, 6, 2):
        ab = struct.unpack(">f", struct.pack(">HH", regs[i],   regs[i+1]))[0]
        ba = struct.unpack(">f", struct.pack(">HH", regs[i+1], regs[i]))[0]
        print(f"  {nom[i//2]:26s} AB CD={ab:10.3f}   CD AB={ba:10.3f}")
else:
    print("Sin respuesta válida — revisa que A/B estén conectados")
