"""Read public configurator API methods through EASE for the named project."""
import sys
loadModule("/FSP/Configuration")
configuration = openDDSCConfiguration(sys.argv[1])
def describe(label, value):
    print("API " + label + " " + str(value.getClass().getName()))
    for method in sorted(str(m.toGenericString()) for m in value.getClass().getMethods()):
        print(method)


describe("configuration", configuration)
describe("bsp", configuration.getBSP())
for instance in configuration.getObjects():
    describe("object", instance)
    break
for thread in configuration.getThreads():
    describe("thread", thread)
    if len(sys.argv) > 2 and thread.isHal():
        # Temporary in-memory stack only: never save this inspection configuration.
        module = thread.createStack(sys.argv[2])
        describe("new stack", module)
        properties = module.getProperties()
        if len(properties):
            describe("property", properties[0])
    break
