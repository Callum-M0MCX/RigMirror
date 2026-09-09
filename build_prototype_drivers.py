"""Rebuild the bundled v0.3.002 candidate radio-driver fleet."""
import json
from pathlib import Path

ROOT = Path(__file__).parent / "drivers"
RADIO_MIN, RADIO_MAX = 30_000, 60_000_000
COMMON = {"1":"LSB", "2":"USB", "3":"CW", "4":"FM", "5":"AM", "6":"FSK", "7":"CW-R", "9":"FSK-R"}

def query(command, pattern, decode=None, mapping=None):
    extract = {"type":"regex_group", "group":"value"}
    if decode: extract["decode"] = decode
    if mapping: extract["map"] = mapping
    return {"kind":"query", "command":command, "response_regex":pattern, "extract":extract}

def receiver(operation, main, sub):
    operation.pop("command", None)
    operation.update(selector="receiver", command_by_selector={"MAIN":main, "SUB":sub})
    return operation

def base(make, model, protocol, identity, topology="selected_vfo"):
    name = f"{make} {model}"
    return {
        "format":"RigMirror Radio Driver", "schema_version":1,
        "metadata":{"manufacturer":make, "model":model, "display_name":name,
                    "driver_revision":1, "release_channel":"CANDIDATE",
                    "notes":"v0.3.002 candidate. TEST DRIVER is optional and its result is local to the exact file."},
        "transport":{"protocol":protocol, "default_baud":38400 if make == "Yaesu" else 19200,
                     "stop_bits":1, "timeout_seconds":0.8, "initialise_commands":[], "shutdown_commands":[]},
        "topology":{"type":topology, "default_receiver":"MAIN"},
        "capabilities":{"identify":{"kind":"query", "command":"ID;" if make == "Yaesu" else "1900",
                                      "response_equals":identity, "extract":{"type":"constant", "value":name}}},
        "revision_history":[{"revision":1, "note":"Initial documentation-derived candidate profile."}],
    }

def yaesu(model, ident, width=9, dual=False, antennas=()):
    d = base("Yaesu", model, "semicolon_ascii", ident, "dual_receiver" if dual else "selected_vfo")
    d["transport"]["initialise_commands"] = ["AI0;"]
    c = d["capabilities"]
    modes = dict(COMMON, **{"8":"LSB", "A":"FM", "B":"FM", "C":"USB"})
    if width == 9: modes.update({"D":"AM", "E":"USB", "F":"FM"})
    reverse = {label:code for code,label in modes.items()}
    if dual:
        c["frequency_read"] = receiver(query("", rf"F[AB](?P<value>\d{{{width}}});", "integer"), "FA;", "FB;")
        c["frequency_write"] = receiver({"kind":"set", "input":{"type":"integer", "minimum":RADIO_MIN, "maximum":RADIO_MAX}}, f"FA{{value:0{width}d}};", f"FB{{value:0{width}d}};")
        c["mode_read"] = receiver(query("", r"MD[01](?P<value>[0-9A-F]);", mapping=modes), "MD0;", "MD1;")
        c["mode_write"] = receiver({"kind":"set", "preserve_wire_value_from":"mode_read", "input":{"type":"enum", "map":reverse}}, "MD0{value};", "MD1{value};")
    else:
        c["frequency_read"] = query("FA;", rf"FA(?P<value>\d{{{width}}});", "integer")
        c["frequency_write"] = {"kind":"set", "command":f"FA{{value:0{width}d}};", "input":{"type":"integer", "minimum":RADIO_MIN, "maximum":RADIO_MAX}}
        c["mode_read"] = query("MD0;", r"MD0(?P<value>[0-9A-F]);", mapping=modes)
        c["mode_write"] = {"kind":"set", "command":"MD0{value};", "preserve_wire_value_from":"mode_read", "input":{"type":"enum", "map":reverse}}
    c["tx_state"] = query("TX;", r"TX(?P<value>[012]);", mapping={"0":"RX", "1":"TX", "2":"TX"})
    if antennas:
        c["antenna_read"] = query("AN0;", r"AN0(?P<value>[1-5]);", mapping={str(i):v for i,v in enumerate(antennas,1)})
        c["antenna_select"] = {"kind":"choice_set", "choices":{v:[f"AN0{i};"] for i,v in enumerate(antennas,1)}}
    d["sources"] = ["Yaesu CAT operation reference: ID, FA/FB, MD, TX and AN commands."]
    return d

def icom(model, address, dual=False, modern=True, antenna=None):
    d = base("Icom", model, "icom_civ", "1900"+address, "dual_receiver" if dual else "selected_vfo")
    d["transport"].update(civ_address=address, controller_address="E0")
    c = d["capabilities"]
    modes = {"00":"LSB", "01":"USB", "02":"AM", "03":"CW", "04":"FSK", "05":"FM", "07":"CW-R", "08":"FSK-R"}
    reverse = {v:k for k,v in modes.items()}
    if modern:
        c["frequency_read"] = query("2500", r"25(?:00|01)(?P<value>[0-9A-F]{10})", "bcd_le")
        c["frequency_write"] = {"kind":"set", "command":"2500{value}", "input":{"type":"bcd_le", "bytes":5}}
        c["mode_read"] = query("2600", r"26(?:00|01)(?P<value>[0-9A-F]{2})[0-9A-F]{2}0[123]", mapping=modes)
        c["mode_write"] = {"kind":"set", "command":"2600{value}", "input":{"type":"enum", "map":reverse}, "preserve_mode_suffix":True}
        if dual:
            for key,prefix,suffix in (("frequency_read","25",""),("frequency_write","25","{value}"),("mode_read","26",""),("mode_write","26","{value}")):
                receiver(c[key], prefix+"00"+suffix, prefix+"01"+suffix)
    else:
        c["frequency_read"] = query("03", r"03(?P<value>[0-9A-F]{10})", "bcd_le")
        c["frequency_write"] = {"kind":"set", "command":"05{value}", "input":{"type":"bcd_le", "bytes":5}}
        c["mode_read"] = query("04", r"04(?P<value>[0-9A-F]{2})(?:[0-9A-F]{2})?", mapping=modes)
        c["mode_write"] = {"kind":"set", "command":"06{value}", "input":{"type":"enum", "map":reverse}}
    c["tx_state"] = query("1C00", r"1C00(?P<value>0[01])", mapping={"00":"RX", "01":"TX"})
    if antenna == "standard2":
        c["antenna_read"] = query("12", r"12(?P<value>0[01])", mapping={"00":"ANT1", "01":"ANT2"})
        c["antenna_select"] = {"kind":"choice_set", "choices":{"ANT1":["1200"], "ANT2":["1201"]}}
    elif antenna == "7300mk2_rx":
        c["antenna_read"] = query("1200", r"1200(?P<value>0[01])", mapping={"00":"ANT1", "01":"RX ANT"})
        c["antenna_select"] = {"kind":"choice_set", "choices":{"ANT1":["120000"], "RX ANT":["120001"]}}
    d["sources"] = ["Icom CI-V command reference: 19 00, frequency, mode, 1C 00 and routing commands."]
    return d

def add_labels(driver):
    for key, operation in driver["capabilities"].items():
        operation.setdefault("label", key.replace("_", " ").capitalize())
    return driver

def upgrade_kenwoods():
    for path in ROOT.glob("Kenwood-*.rmradio"):
        d = json.loads(path.read_text(encoding="utf-8"))
        d.pop("rigmirror_validation", None)
        m = d["metadata"]
        m["driver_revision"] = 3
        m["release_channel"] = "CANDIDATE"
        m["notes"] = "v0.3.002 candidate. TEST DRIVER is optional and its result is tied to this exact file."
        for op in d["capabilities"].values():
            spec = op.get("input")
            if isinstance(spec, dict) and spec.get("type") == "integer": spec["maximum"] = RADIO_MAX
        if d.get("topology",{}).get("type") == "active_vfo":
            c = d["capabilities"]
            c["transmit_vfo"] = query("FT;", r"FT(?P<value>[01]);", mapping={"0":"VFO A", "1":"VFO B"})
            c["transmit_vfo_write"] = {"kind":"set", "command":"FT{value};", "input":{"type":"mapped_choice", "map":{"VFO A":"0", "VFO B":"1"}}}
            c["tx_frequency_read"] = {"kind":"query", "selector":"transmit_vfo", "command_by_selector":{"VFO A":"FA;", "VFO B":"FB;"}, "response_regex":r"F[AB](?P<value>\d{11});", "extract":{"type":"regex_group", "group":"value", "decode":"integer"}}
            c["tx_frequency_write"] = {"kind":"set", "selector":"transmit_vfo", "command_by_selector":{"VFO A":"FA{value:011d};", "VFO B":"FB{value:011d};"}, "input":{"type":"integer", "minimum":RADIO_MIN, "maximum":RADIO_MAX}}
        d.setdefault("revision_history", []).append({"revision":3, "note":"v0.3.002: physical 6 m range and pre-PTT M transmit-VFO preparation."})
        path.write_text(json.dumps(add_labels(d), indent=2)+"\n", encoding="utf-8")
    source = ROOT/"Kenwood-TS-590SG.rmradio"
    d = json.loads(source.read_text(encoding="utf-8"))
    d["metadata"].update(model="TS-590S", display_name="Kenwood TS-590S", driver_revision=3)
    d["capabilities"]["identify"].update(response_equals="ID021;", extract={"type":"constant", "value":"Kenwood TS-590S"})
    (ROOT/"Kenwood-TS-590S.rmradio").write_text(json.dumps(d,indent=2)+"\n",encoding="utf-8")

def write(driver, filename=None):
    add_labels(driver)
    filename = filename or f'{driver["metadata"]["manufacturer"]}-{driver["metadata"]["model"]}.rmradio'
    (ROOT/filename).write_text(json.dumps(driver,indent=2)+"\n",encoding="utf-8")

if __name__ == "__main__":
    upgrade_kenwoods()
    fleet = [
        (yaesu("FTDX101MP","ID0682;",9,True,("ANT1","ANT2","ANT3")),None),
        (yaesu("FTDX101D","ID0681;",9,True,("ANT1","ANT2","ANT3")),None),
        (yaesu("FTDX10","ID0761;"),None),
        (yaesu("FT-710 / FT-710 AESS","ID0800;"),"Yaesu-FT-710-AESS.rmradio"),
        (yaesu("FT-991A","ID0670;"),None), (yaesu("FT-891","ID0135;"),None),
        (yaesu("FTDX5000 / D / MP","ID0362;",8,True,("ANT1","ANT2","ANT3","RX ANT")),"Yaesu-FTDX5000-D-MP.rmradio"),
        (yaesu("FTDX3000","ID0462;",8),None),
        (yaesu("FT-2000 / FT-2000D","ID0251;",8,False,("ANT1","ANT2")),"Yaesu-FT-2000-D.rmradio"),
        (yaesu("FT-950","ID0310;",8,False,("ANT1","ANT2")),None),
        (icom("IC-7300","94"),None), (icom("IC-7300MK2","B6",antenna="7300mk2_rx"),None),
        (icom("IC-7610","98",True,True,"standard2"),None), (icom("IC-705","A4"),None),
        (icom("IC-7100","88",modern=False),None),
        (icom("IC-7850 / IC-7851","8E",True),"Icom-IC-7850-7851.rmradio"),
        (icom("IC-7700","74",modern=False),None), (icom("IC-7760","B2",True),None),
        (icom("IC-9100","7C",modern=False),None),
    ]
    for driver, filename in fleet: write(driver, filename)
