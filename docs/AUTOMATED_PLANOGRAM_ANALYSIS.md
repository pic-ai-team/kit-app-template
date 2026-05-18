# Automated Planogram Analysis Architecture

This document describes the design and implementation of the automated planogram analysis feature

### Stages

#### V1: Shelf stock analysis with hidden camera (No planogram compliance)
##### Camera logic
A hidden camera is spawned the same way as the **cctv cameras** in `cctv_capture.py` file. It jumps along a defined route (can be configured in UI) using the existing route logic with a denotation `planogram_analysis_<route_name>` (similar concept to navigation points for cctv cameras e.g., `cctv_drinks_aisle`). 

##### Shelf stock analysis logic
Currently there are 2 planogram analysis functions. The old one `_run_planogram_analysis` and the new one `_run_shelf_analysis`. We want to utilize the new version `_run_shelf_analysis`. We create a new function called `_run_automatic_shelf_analysis`: It should take a route instead of just the viewport. 
At each route position, a camera is spawned at the coordinates and the camera captures a frame and sends a request to the agent backend `/api/identify-shelf-products` endpoint. 

`/api/identify-shelf-products` returns object:
```python
"asset_keys": ["pringles_bbq", "pringles_cheese", "pringles_pizza", "cheetos_double_cheese"],
```

`_run_shelf_analysis` return object:
```python
{
    "products": ["pringles_bbq", "pringles_cheese", "pringles_pizza", "cheetos_double_cheese"],
    "shelf_levels": [
        {
            "level": 1,
            "floor_z": 114.08,
            "products": {
                "pringles_cheese": {
                    "count": 8
                },
                "pringles_pizza": {
                    "count": 8
                },
                "pringles_bbq": {
                    "count": 8
                },
                "cheetos_double_cheese": {
                    "count": 8
                }
            }
        }
    ]
}
```
=> Shelf analysis should be able to provide more information
1. Current stock vs initial stock (Like 20% left)
2. Shelf name (No need to implement yet)
=> `/api/identify-shelf-products` has to return richer information. It should for each asset_key return their info using `db.get_catalog_entry(asset_key)`:
=> The Shelf analysis can calculate the stock ration like 20% left using the count generated in `run_shelf_analysis` and the `init_stock` from `api/identify-shelf-products`


##### Accessing automated shelf analyzis
- **Via the Webviewer:** The Planogram Analysis Tab should be extended at the top with a new section called: Automatic Shelf Analysis. On the left should be a dropdown where the user can select one of the available routes (filtered for all with the `planogram_analysis_` prefix). Then there is a run button that will show a loading spinner while the analysis is running. The result will be rendered as a list of navigation points. For each navigation point (frame) there is a list of products, and their status like stocked 32/32 or medium 18/32 low 3/32. 
- **Via the Kit API Server:**: A new api enpoint called /planogram/shelf-analysis which takes a route as a parameter and will perform the `_run_automatic_shelf_analysis` function and return the result as json

