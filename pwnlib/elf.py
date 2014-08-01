import pwnlib
import mmap
from os.path import abspath
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import Section, SymbolTableSection
from elftools.elf.relocation import RelocationSection
from elftools.elf.descriptions import describe_ei_class, describe_e_type
from elftools.elf.constants import P_FLAGS

def load(*args, **kwargs):
    """Compatibility wrapper for pwntools v1"""
    return ELF(*args, **kwargs)

class ELF(ELFFile):
    """Encapsulates information about an ELF file.

    :ivar path: Path to the binary on disk
    :ivar symbols:  Dictionary of {name: address} for all symbols in the ELF
    :ivar plt:      Dictionary of {name: address} for all functions in the PLT
    :ivar got:      Dictionary of {name: address} for all function pointers in the GOT
    :ivar libs:     Dictionary of {path: address} for each shared object required to load the ELF
    """
    def __init__(self, path):
        # elftools uses the backing file for all reads and writes
        # in order to permit writing without being able to write to disk,
        # mmap() the file.
        self.file = open(path,'rb')
        self.mmap = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_COPY)

        super(ELF,self).__init__(self.mmap)

        self.path     = abspath(path)

        self._populate_got_plt()
        self._populate_symbols()
        self._populate_libraries()

        self._address  = min(filter(bool, (s.header.p_vaddr for s in self.segments)))

    @property
    def elfclass(self):
        """ELF class (32 or 64).

        note::
            Set during ELFFile._identify_file
        """
        return self._elfclass

    @elfclass.setter
    def elfclass(self, newvalue):
        self._elfclass = newvalue

    @property
    def elftype(self):
        """ELF type (EXEC, DYN, etc)"""
        return describe_e_type(self.header.e_type).split()[0]

    @property
    def segments(self):
        """A list of all segments in the ELF"""
        return list(self.iter_segments())

    @property
    def sections(self):
        """A list of all sections in the ELF"""
        return list(self.iter_sections())

    @property
    def dwarf(self):
        """DWARF info for the elf"""
        return self.get_dwarf_info()

    @property
    def address(self):
        """Address of the lowest segment loaded in the ELF.
        When updated, cascades updates to segment vaddrs, section addrs, symbols, plt, and got.

        >>> bash = ELF('/bin/sh')
        >>> old = bash.symbols['read']
        >>> bash.address += 0x1000
        >>> bash.symbols['read'] == old + 0x1000
        True
        """
        return self._address

    @address.setter
    def address(self, new):
        delta     = new-self._address
        update    = lambda x: x+delta

        for segment in self.segments:
            segment.header.p_vaddr += delta

        for section in self.sections:
            section.header.sh_addr += delta

        self.symbols = {k:update(v) for k,v in self.symbols.items()}
        self.plt     = {k:update(v) for k,v in self.plt.items()}
        self.got     = {k:update(v) for k,v in self.got.items()}

        self._address = update(self.address)

    def section(self, name):
        """Gets data for the named section

        Args:
            name(str): Name of the section

        Returns:
            String containing the bytes for that section
        """
        return self.get_section_by_name(name).data()

    @property
    def executable_segments(self):
        """Returns: list of all segments which are executable."""
        return [s for s in self.segments if s.header.p_flags & P_FLAGS.PF_X]

    @property
    def writable_segments(self):
        """Returns: list of all segments which are writeable"""
        return [s for s in self.segments if s.header.p_flags & P_FLAGS.PF_W]


    def _populate_libraries(self, processlike=None):
        self.libs = ldd(self.path, processlike)

    def _populate_symbols(self):
        # By default, have 'symbols' include everything in the PLT.
        #
        # This way, elf.symbols['write'] will be a valid address to call
        # for write().
        self.symbols = dict(self.plt)

        for section in self.sections:
            if not isinstance(section, SymbolTableSection):
                continue

            for symbol in section.iter_symbols():
                if not symbol.entry.st_value:
                    continue

                self.symbols[symbol.name] = symbol.entry.st_value

    def _populate_got_plt(self):
        plt = self.get_section_by_name('.plt')
        got = self.get_section_by_name('.got')

        # Find the relocation section for PLT
        rel_plt = next(s for s in self.sections if s.header.sh_info == self.sections.index(plt))

        # Find the symbols for the relocation section
        sym_rel_plt = self.sections[rel_plt.header.sh_link]

        self.got = {}
        self.plt = {}

        for rel in rel_plt.iter_relocations():
            sym_idx  = rel.entry.r_info_sym
            symbol   = sym_rel_plt.get_symbol(sym_idx)
            name     = symbol.name

            self.got[name] = rel.entry.r_offset
            self.plt[name] = plt.header.sh_addr + sym_idx*plt.header.sh_addralign

    def search(self, s, non_writable = False):
        """Search the ELF's virtual address space for the specified string.

        Args:
            s(str): String to search for
            non_writable(bool): Search non-writable sections

        Returns:
            An iterator for each virtual address that matches

        Examples:
            >>> bash = ELF('/bin/bash')
            >>> bash.address + 1 == next(bash.search('ELF',True))
            True
        """

        if non_writable:    segments = self.segments
        else:               segments = self.writable_segments

        for seg in segments:
            addr   = seg.header.p_vaddr
            data   = seg.data()
            offset = 0
            while True:
                offset = data.find(s, offset)
                if offset == -1:
                    break
                yield addr + offset
                offset += 1

    def offset_to_vaddr(self, offset):
        """Translates the specified offset to a virtual address.

        Args:
            offset(int): Offset to translate

        Returns:
            Virtual address which corresponds to the file offset, or None

        Examples:
            >>> bash = ELF('/bin/bash')
            >>> bash.address == bash.offset_to_vaddr(0)
            True
        """
        for segment in self.segments:
            begin = segment.header.p_offset
            size  = segment.header.p_filesz
            end   = begin + size
            if begin <= offset and offset <= end:
                delta = offset - begin
                return segment.header.p_vaddr + delta
        return None


    def vaddr_to_offset(self, address):
        """Translates the specified virtual address to a file address

        Args:
            address(int): Virtual address to translate

        Returns:
            Offset within the ELF file which corresponds to the address,
            or None.

        Examples:
            >>> bash = ELF('/bin/bash')
            >>> 0 == bash.vaddr_to_offset(bash.address)
            True
        """
        for segment in self.segments:
            begin = segment.header.p_vaddr
            size  = segment.header.p_memsz
            end   = begin + size
            if begin <= address and address <= end:
                delta = address - begin
                return segment.header.p_offset + delta
        return None

    def read(self, address, count):
        """Read data from the specified virtual address

        Args:
            address(int): Virtual address to read
            count(int): Number of bytes to read

        Returns:
            A string of bytes, or None

        Examples:
          >>> bash = ELF('/bin/bash')
          >>> bash.read(bash.address+1, 3)
          'ELF'
        """
        offset = self.vaddr_to_offset(address)

        if offset is not None:
            old = self.stream.tell()
            self.stream.seek(offset)
            data = self.stream.read(count)
            self.stream.seek(old)
            return data

        return None

    def write(self, address, data):
        """Writes data to the specified virtual address

        Args:
            address(int): Virtual address to write
            data(str): Bytes to write

        Note::
            This routine does not check the bounds on the write to ensure
            that it stays in the same segment.

        Examples:
          >>> bash = ELF('/bin/bash')
          >>> bash.read(bash.address+1, 3)
          'ELF'
          >>> bash.write(bash.address, "HELO")
          >>> bash.read(bash.address, 4)
          'HELO'
        """
        offset = self.vaddr_to_offset(address)

        if offset is not None:
            old = self.stream.tell()
            self.stream.seek(offset)
            self.stream.write(data)
            self.stream.seek(old)

        return None



def ldd(path, tube=None):
    """Effectively runs 'ldd' on the specified binary, captures the output,
    and parses it.  Returns a dictionary of {path: address} for
    each library required by the specified binary.

    Args:
      path(str): Path to the binary
      tube(callable): Callable which behaves like a pwn.tubes.process.process.
            Exists to allow compatibility with ssh.run.

    Example:
        > ldd('/bin/bash')
        {'/lib/x86_64-linux-gnu/libc.so.6': 139641095565312,
         '/lib/x86_64-linux-gnu/libdl.so.2': 139641099526144,
         '/lib/x86_64-linux-gnu/libtinfo.so.5': 139641101639680}
    """
    import re
    expr = re.compile(r'\s(\S?/\S+)\s+\((0x.+)\)')
    libs = {}

    if tube is None:
        from pwnlib.tubes.process import process
        tube = process

    output = tube([path],env={'LD_TRACE_LOADED_OBJECTS':'1'}).recvall().strip().splitlines()
    output = map(str.strip, output)
    output = map(expr.search, output)

    for match in filter(None, output):
        lib, addr = match.groups()
        libs[lib] = int(addr,16)

    return libs

if __name__ == "__main__":
    import doctest
    doctest.testmod()