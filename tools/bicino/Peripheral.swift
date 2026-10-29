// CoreBluetooth peripheral adapter. All protocol handlers and credentials stay in the guest.
import Foundation
import CoreBluetooth

func emit(_ message: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: message) else { return }
    FileHandle.standardOutput.write(data + Data([10]))
}
func hex(_ data: Data) -> String { data.map { String(format: "%02x", $0) }.joined() }
func unhex(_ text: String) -> Data? {
    guard text.count % 2 == 0 else { return nil }
    var result = Data(), index = text.startIndex
    while index < text.endIndex {
        let end = text.index(index, offsetBy: 2)
        guard let byte = UInt8(text[index..<end], radix: 16) else { return nil }
        result.append(byte); index = end
    }
    return result
}

final class Peripheral: NSObject, CBPeripheralManagerDelegate {
    var manager: CBPeripheralManager!
    var specs: [[String: Any]] = []
    var serviceUUID = "", name = "BikeComputer SIM"
    var characteristics: [Int: CBMutableCharacteristic] = [:]
    var handles: [CBUUID: Int] = [:]
    var subscribers: [CBUUID: CBCentral] = [:]
    var centralID: UUID?
    var nextID = 0
    var pending: [Int: (CBATTRequest, Date)] = [:]
    var notifications: [(Int, Data)] = []
    var serviceAdded = false

    func configure(_ message: [String: Any]) {
        guard manager == nil, let service = message["service"] as? [String: Any],
              let uuid = service["uuid"] as? String,
              let specs = service["characteristics"] as? [[String: Any]] else { fatal("invalid configuration"); return }
        self.specs = specs; serviceUUID = uuid
        name = message["name"] as? String ?? name
        manager = CBPeripheralManager(delegate: self, queue: .main)
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self else { return }
            if self.pending.values.contains(where: { Date().timeIntervalSince($0.1) > 10 }) {
                self.fatal("guest request timed out")
            }
        }
    }
    func fatal(_ reason: String) {
        emit(["t": "fatal", "message": reason])
        if let manager {
            manager.stopAdvertising()
            for request in pending.values { manager.respond(to: request.0, withResult: .unlikelyError) }
            pending.removeAll(); manager.removeAllServices()
        }
        exit(1)
    }
    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        emit(["t": "bluetooth", "state": peripheral.state.rawValue])
        guard peripheral.state == .poweredOn else {
            if serviceAdded { fatal("Bluetooth power/state changed; restart the bridge for a fresh guest session"); return }
            if peripheral.state == .unauthorized || peripheral.state == .unsupported { fatal("Bluetooth peripheral unavailable or not authorized") }
            return
        }
        guard !serviceAdded else { return }
        let service = CBMutableService(type: CBUUID(string: serviceUUID), primary: true)
        var values: [CBMutableCharacteristic] = []
        for spec in specs {
            guard let uuid = spec["uuid"] as? String, let handle = spec["handle"] as? Int,
                  let flags = spec["properties"] as? Int else { fatal("invalid GATT characteristic"); return }
            var properties: CBCharacteristicProperties = []
            var permissions: CBAttributePermissions = []
            if flags & 2 != 0 { properties.insert(.read); permissions.insert(.readable) }
            if flags & 4 != 0 { properties.insert(.writeWithoutResponse); permissions.insert(.writeable) }
            if flags & 8 != 0 { properties.insert(.write); permissions.insert(.writeable) }
            if flags & 16 != 0 { properties.insert(.notify) }
            if flags & 32 != 0 { properties.insert(.indicate) }
            let value = CBMutableCharacteristic(type: CBUUID(string: uuid), properties: properties, value: nil, permissions: permissions)
            values.append(value); characteristics[handle] = value; handles[value.uuid] = handle
        }
        service.characteristics = values
        peripheral.add(service)
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, didAdd service: CBService, error: Error?) {
        if let error { fatal("add service: \(error.localizedDescription)"); return }
        serviceAdded = true; advertise()
    }
    func advertise() {
        manager.startAdvertising([CBAdvertisementDataLocalNameKey: name,
                                  CBAdvertisementDataServiceUUIDsKey: [CBUUID(string: serviceUUID)]])
    }
    func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
        if let error { fatal("advertising: \(error.localizedDescription)"); return }
        emit(["t": "advertising", "name": name])
    }
    func accept(_ central: CBCentral) -> Bool {
        if let centralID, centralID != central.identifier { return false }
        centralID = central.identifier
        manager.stopAdvertising()
        return true
    }
    func request(_ request: CBATTRequest, kind: String) {
        guard accept(request.central), let handle = handles[request.characteristic.uuid] else {
            manager.respond(to: request, withResult: .insufficientResources); return
        }
        nextID += 1; pending[nextID] = (request, Date())
        emit(["t": kind, "id": nextID, "handle": handle, "offset": request.offset,
              "value": hex(request.value ?? Data()), "bytes": request.value?.count ?? 0,
              "mtu": request.central.maximumUpdateValueLength + 3])
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveRead request: CBATTRequest) { self.request(request, kind: "read") }
    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveWrite requests: [CBATTRequest]) {
        guard let first = requests.first else { return }
        // CoreBluetooth asks for one response per callback. Refuse compound/offset writes
        // rather than acknowledge a partly forwarded application command.
        guard requests.count == 1, first.offset == 0 else {
            peripheral.respond(to: first, withResult: .requestNotSupported); return
        }
        request(first, kind: "write")
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didSubscribeTo characteristic: CBCharacteristic) {
        guard accept(central), let handle = handles[characteristic.uuid] else { fatal("second central or unknown subscription"); return }
        subscribers[characteristic.uuid] = central
        emit(["t": "subscribe", "handle": handle, "mtu": central.maximumUpdateValueLength + 3])
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didUnsubscribeFrom characteristic: CBCharacteristic) {
        subscribers.removeValue(forKey: characteristic.uuid)
        emit(["t": "unsubscribe", "handle": handles[characteristic.uuid] ?? 0])
        if subscribers.isEmpty {
            centralID = nil; notifications.removeAll()
            // Host advertises again only after Python confirms guest teardown.
            emit(["t": "disconnect"])
        }
    }
    func receive(_ message: [String: Any]) {
        switch message["t"] as? String {
        case "configure": configure(message)
        case "reply":
            guard let id = message["id"] as? Int, let (request, _) = pending.removeValue(forKey: id) else { return }
            let code = message["error"] as? Int ?? 0
            if let value = message["value"] as? String { request.value = unhex(value) }
            manager.respond(to: request, withResult: CBATTError.Code(rawValue: code) ?? .unlikelyError)
            emit(["t": "replied", "id": id, "error": code])
        case "notify":
            guard let handle = message["handle"] as? Int, let text = message["value"] as? String, let data = unhex(text) else { fatal("invalid notification"); return }
            guard notifications.count < 128 else { fatal("notification queue overflow"); return }
            notifications.append((handle, data)); flush()
        case "disconnected": advertise()
        case "shutdown":
            manager?.stopAdvertising(); manager?.removeAllServices()
            exit(0)
        case "fatal": fatal(message["message"] as? String ?? "guest bridge stopped")
        default: break
        }
    }
    func flush() {
        while let (handle, data) = notifications.first {
            guard let characteristic = characteristics[handle], let central = subscribers[characteristic.uuid] else {
                notifications.removeFirst(); continue
            }
            guard data.count <= central.maximumUpdateValueLength else { fatal("guest notification exceeds physical central MTU"); return }
            if !manager.updateValue(data, for: characteristic, onSubscribedCentrals: [central]) { return }
            notifications.removeFirst()
        }
    }
    func peripheralManagerIsReady(toUpdateSubscribers peripheral: CBPeripheralManager) { flush() }
}

emit(["t": "started", "pid": ProcessInfo.processInfo.processIdentifier])
let peripheral = Peripheral()
DispatchQueue.global().async {
    while let line = readLine() {
        guard let data = line.data(using: .utf8), let message = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
        DispatchQueue.main.async { peripheral.receive(message) }
    }
    DispatchQueue.main.async { peripheral.fatal("transport closed") }
}
RunLoop.main.run()
