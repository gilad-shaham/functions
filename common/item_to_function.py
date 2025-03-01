from optparse import OptionParser
from pathlib import Path
from typing import Optional

from mlrun import code_to_function
from yaml import full_load


def item_to_function(item_path: str = ".", output_path: Optional[str] = None):
    item_path = Path(item_path)

    if item_path.is_dir():
        if (item_path / "item.yaml").exists():
            item_path = item_path / "item.yaml"
        else:
            raise FileNotFoundError(f"{item_path} does not contain a item.yaml file")
    elif not item_path.exists():
        raise FileNotFoundError(f"{item_path} not found")

    item_yaml = full_load(open(item_path, "r"))

    filename = item_yaml.get("spec", {}).get("filename")

    code_output = ""
    if filename.endswith(".ipynb"):
        code_output = Path(filename)
        code_output = code_output.parent / f"{code_output.stem}.py"

    function_object = code_to_function(
        name=item_yaml["name"],
        filename=item_yaml.get("spec", {}).get("filename"),
        handler=item_yaml.get("spec", {}).get("handler"),
        kind=item_yaml.get("spec", {}).get("kind"),
        code_output=code_output,
        image=item_yaml.get("spec", {}).get("image"),
        description=item_yaml.get("description", ""),
        requirements=item_yaml.get("spec", {}).get("requirements"),
        categories=item_yaml.get("categories", []),
        labels=item_yaml.get("labels", {}),
    )

    if output_path is None:
        return function_object

    output_path = Path(output_path)

    if output_path.is_dir():
        output_path = output_path / "function.yaml"

    if not output_path.parent.exists():
        output_path.mkdir()

    function_object.export(target=output_path.absolute())


if __name__ == "__main__":
    parser = OptionParser()
    parser.add_option("-i", "--item", help="Path to item.yaml file")
    parser.add_option("-o", "--output", help="Output path for function.yaml")
    options, args = parser.parse_args()
    item_to_function(item_path=options.item, output_path=options.output)
