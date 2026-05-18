BT_PLIST=/tmp/bt_rfcomm_info.plist

bt_rfcomm: bt_rfcomm.swift
	@printf '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n<key>CFBundleIdentifier</key><string>com.local.bt-rfcomm</string>\n<key>NSBluetoothAlwaysUsageDescription</key><string>Needed for PT-P300BT label printer.</string>\n</dict></plist>\n' > $(BT_PLIST)
	swiftc -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker $(BT_PLIST) -o $@ $<
	@echo "Built bt_rfcomm"

clean:
	rm -f bt_rfcomm

.PHONY: clean
