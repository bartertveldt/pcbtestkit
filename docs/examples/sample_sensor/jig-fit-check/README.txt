These files show the jigs in their working position on the board, in KiCad's own 3D coordinates
(the same as KiCad's File > Export > STL of the board), so they line up when opened together:

  <board>-<side>-board.stl            the board and every part on it during that pass (KiCad's 3D shapes)
  <board>-<side>-holding-jig.stl      the holding jig, turned over onto the board
  <board>-<side>-placement-jig.stl    the placement jig under the board
  <board>-paste-nest-<side>-*.stl     an SMD pass's paste-print nest and the board on it

Move a jig straight away from the board and back: nothing touches until it is home, and then every hand-soldered
part touches its pocket floor at the moment the board's face meets the support. KiCad draws the board body
slightly thinner than the board (copper and solder mask are separate layers); parts sit on the board's faces, and
the jigs are placed on those faces too. Print the jig STLs next to this folder, not these copies.
